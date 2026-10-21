/*
 * Shared KeDei V6.2 LCD/XPT2046 transport, using the current Linux ABI.
 *
 * Required: spi0-1cs,cs0_pin=7, no other SPI0 device, and access through the
 * spi/gpio groups to /dev/spidev0.0 and the pinctrl-bcm2835 GPIO chip.
 * BCM7 is kernel-controlled LCD CS; BCM8 is the LCD latch / manual touch CS.
 * One handle owns BCM8, BCM25 and an exclusive advisory spidev lock. All bus
 * operations use one mutex, so touch cannot interrupt a full LCD frame group.
 *
 * Protocol reference:
 * https://github.com/lzto/RaspberryPi_KeDei_35_lcd_v62
 * https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/input/touchscreen/ads7846.c
 * LCD initialization and drawing remain in the Python caller.
 *
 * Build: cc -shared -fPIC -O2 -Wall -Wextra -Werror -pthread \
 *           -o _kedei_transport.so _kedei_transport.c
 *
 * ctypes ABI (load with use_errno=True):
 *   void *kedei_open(uint32_t speed_hz);       NULL on error; 0 means 4 MHz
 *   int kedei_write_frames(void *, const uint8_t *, size_t frame_len, size_t count);
 *   int kedei_read_touch(void *, int *x, int *y, int *z1, int *z2);
 *   int kedei_close(void *);
 * write/close return 0 or -1. Touch returns 0 (pen up), 1 (valid press),
 * 2 (pen down but invalid ADC sample), or -1 (error).
 * Every failure sets errno for the caller's OSError/strerror. Stop and join
 * reader threads before close: callers must not use a freed handle.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/gpio.h>
#include <linux/spi/spidev.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef GPIO_V2_GET_LINE_IOCTL
#error "Linux GPIO character-device v2 headers are required"
#endif

#define SPI_DEVICE "/dev/spidev0.0"
#define TOUCH_SPEED 500000u

struct kedei_transport {
    int spi_fd, chip_fd, latch_fd, penirq_fd;
    uint32_t speed, old_mode, old_speed;
    uint8_t old_bpw;
    bool restore_spi;
    bool lcd_parked;
    pthread_mutex_t mutex;
};

static int ioctl_retry(int fd, unsigned long request, void *arg)
{
    int result;
    do {
        result = ioctl(fd, request, arg);
    } while (result < 0 && errno == EINTR);
    return result;
}

static uint32_t be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
}

static int verify_spi_mapping(void)
{
    static const char *paths[] = {
        "/sys/class/spi_master/spi0/device/of_node/cs-gpios",
        "/proc/device-tree/soc/spi@7e204000/cs-gpios"
    };
    uint8_t property[16];
    ssize_t length;
    int fd = -1, saved_errno;
    size_t i;
    for (i = 0; i < sizeof(paths) / sizeof(paths[0]); ++i) {
        fd = open(paths[i], O_RDONLY | O_CLOEXEC);
        if (fd >= 0)
            break;
    }
    if (fd < 0)
        return -1;
    do {
        length = read(fd, property, sizeof(property));
    } while (length < 0 && errno == EINTR);
    saved_errno = errno;
    close(fd);
    if (length < 0) {
        errno = saved_errno;
        return -1;
    }
    if (length != 12 || be32(property) == 0 || be32(property + 4) != 7 ||
        be32(property + 8) != 1) {
        errno = EINVAL;
        return -1;
    }
    return 0;
}

static int verify_spi_devices(void)
{
    DIR *devices = opendir("/sys/bus/spi/devices");
    struct dirent *entry;
    bool found = false;
    int result = -1, saved_errno = ENODEV;
    if (!devices)
        return -1;
    while ((entry = readdir(devices)) != NULL) {
        char path[PATH_MAX], resolved[PATH_MAX];
        const char *driver;
        if (strncmp(entry->d_name, "spi0.", 5) != 0)
            continue;
        if (strcmp(entry->d_name, "spi0.0") != 0) {
            saved_errno = EBUSY;
            goto done;
        }
        if (snprintf(path, sizeof(path), "/sys/bus/spi/devices/%s/driver",
                     entry->d_name) >= (int)sizeof(path)) {
            saved_errno = ENAMETOOLONG;
            goto done;
        }
        if (!realpath(path, resolved)) {
            saved_errno = errno;
            goto done;
        }
        driver = strrchr(resolved, '/');
        driver = driver ? driver + 1 : resolved;
        if (strcmp(driver, "spidev") != 0) {
            saved_errno = EBUSY;
            goto done;
        }
        found = true;
    }
    if (found)
        result = 0;
done:
    closedir(devices);
    if (result < 0)
        errno = saved_errno;
    return result;
}

static int find_gpio_chip(struct kedei_transport *p)
{
    DIR *devices = opendir("/dev");
    struct dirent *entry;
    dev_t selected_device = 0;
    bool found = false;
    int result = -1, saved_errno = ENODEV;
    if (!devices)
        return -1;
    while ((entry = readdir(devices)) != NULL) {
        struct gpiochip_info chip = {0};
        struct stat chip_stat;
        char path[PATH_MAX];
        int fd;
        if (strncmp(entry->d_name, "gpiochip", 8) != 0)
            continue;
        if (snprintf(path, sizeof(path), "/dev/%s", entry->d_name) >=
            (int)sizeof(path)) {
            saved_errno = ENAMETOOLONG;
            goto done;
        }
        fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) {
            saved_errno = errno;
            goto done;
        }
        if (ioctl_retry(fd, GPIO_GET_CHIPINFO_IOCTL, &chip) < 0 ||
            fstat(fd, &chip_stat) < 0) {
            saved_errno = errno;
            close(fd);
            goto done;
        }
        if (strcmp(chip.label, "pinctrl-bcm2835") != 0) {
            close(fd);
            continue;
        }
        if (!S_ISCHR(chip_stat.st_mode)) {
            close(fd);
            saved_errno = ENODEV;
            goto done;
        }
        /* Compatibility symlinks may expose the same chip under two names. */
        if (found && chip_stat.st_rdev == selected_device) {
            close(fd);
            continue;
        }
        if (found || chip.lines <= 25u) {
            close(fd);
            saved_errno = ENODEV;
            goto done;
        }
        p->chip_fd = fd;
        selected_device = chip_stat.st_rdev;
        found = true;
    }
    if (found)
        result = 0;
done:
    closedir(devices);
    if (result < 0)
        errno = saved_errno;
    return result;
}

static int request_line(int chip_fd, unsigned int offset, uint64_t flags,
                        const char *consumer, bool initial_high)
{
    struct gpio_v2_line_info info = {.offset = offset};
    struct gpio_v2_line_request request = {0};
    if (ioctl_retry(chip_fd, GPIO_V2_GET_LINEINFO_IOCTL, &info) < 0)
        return -1;
    if (info.flags & GPIO_V2_LINE_FLAG_USED) {
        errno = EBUSY;
        return -1;
    }
    request.offsets[0] = offset;
    request.num_lines = 1;
    snprintf(request.consumer, sizeof(request.consumer), "%s", consumer);
    request.config.flags = flags;
    if (flags & GPIO_V2_LINE_FLAG_OUTPUT) {
        request.config.num_attrs = 1;
        request.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES;
        request.config.attrs[0].attr.values = initial_high ? 1u : 0u;
        request.config.attrs[0].mask = 1u;
    }
    if (ioctl_retry(chip_fd, GPIO_V2_GET_LINE_IOCTL, &request) < 0)
        return -1;
    if (fcntl(request.fd, F_SETFD, FD_CLOEXEC) < 0) {
        int saved_errno = errno;
        close(request.fd);
        errno = saved_errno;
        return -1;
    }
    return request.fd;
}

static int set_latch(struct kedei_transport *p, bool high)
{
    struct gpio_v2_line_values values = {.bits = high ? 1u : 0u, .mask = 1u};
    return ioctl_retry(p->latch_fd, GPIO_V2_LINE_SET_VALUES_IOCTL, &values);
}

static int set_mode(struct kedei_transport *p, uint32_t mode)
{
    return ioctl_retry(p->spi_fd, SPI_IOC_WR_MODE32, &mode);
}

static int pen_down(struct kedei_transport *p)
{
    struct gpio_v2_line_values values = {.mask = 1u};
    if (ioctl_retry(p->penirq_fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &values) < 0)
        return -1;
    return (values.bits & 1u) == 0;
}

static int lcd_frame(struct kedei_transport *p, const uint8_t *bytes, size_t length)
{
    struct spi_ioc_transfer transfer = {0};
    int sent;
    transfer.tx_buf = (uintptr_t)bytes;
    transfer.len = (uint32_t)length;
    transfer.speed_hz = p->speed;
    transfer.bits_per_word = 8;
    if (set_latch(p, true) < 0)
        return -1;
    sent = ioctl_retry(p->spi_fd, SPI_IOC_MESSAGE(1), &transfer);
    if (sent < 0)
        return -1;
    if ((size_t)sent != length) {
        errno = EIO;
        return -1;
    }
    if (set_latch(p, false) < 0)
        return -1;
    p->lcd_parked = length == 3 && bytes[0] == 0x11 && bytes[1] == 0 && bytes[2] == 0;
    return 0;
}

/* Bring both physical chip selects to idle even after an intermediate error.
 * Changing back to normal mode makes spi_setup explicitly deselect GPIO7.
 */
static int idle_bus(struct kedei_transport *p)
{
    int saved_errno = 0;
    if (p->latch_fd >= 0 && set_latch(p, true) < 0)
        saved_errno = errno;
    if (p->spi_fd >= 0 && p->restore_spi && set_mode(p, SPI_MODE_0) < 0 &&
        saved_errno == 0)
        saved_errno = errno;
    if (saved_errno) {
        errno = saved_errno;
        return -1;
    }
    return 0;
}

static int cleanup(struct kedei_transport *p)
{
    int saved_errno = 0;
    if (idle_bus(p) < 0)
        saved_errno = errno;
    if (p->spi_fd >= 0 && p->restore_spi) {
        if (ioctl_retry(p->spi_fd, SPI_IOC_WR_MODE32, &p->old_mode) < 0 && !saved_errno)
            saved_errno = errno;
        if (ioctl_retry(p->spi_fd, SPI_IOC_WR_BITS_PER_WORD, &p->old_bpw) < 0 && !saved_errno)
            saved_errno = errno;
        if (ioctl_retry(p->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &p->old_speed) < 0 && !saved_errno)
            saved_errno = errno;
    }
    if (p->spi_fd >= 0)
        close(p->spi_fd);
    if (p->latch_fd >= 0)
        close(p->latch_fd);
    if (p->penirq_fd >= 0)
        close(p->penirq_fd);
    if (p->chip_fd >= 0)
        close(p->chip_fd);
    p->spi_fd = p->chip_fd = p->latch_fd = p->penirq_fd = -1;
    if (saved_errno) {
        errno = saved_errno;
        return -1;
    }
    return 0;
}

/* A previous process may have died after a PD1 conversion, leaving PENIRQ
 * disabled. One unconditional PD0 conversion restores IRQ operation before
 * the normal reader begins gating ADC transactions on GPIO25. */
static int prime_touch(struct kedei_transport *p)
{
    static const uint8_t nop[3] = {0x11, 0, 0};
    static const uint8_t powerdown[3] = {0x90, 0, 0};
    struct spi_ioc_transfer transfer = {0};
    int result = -1, saved_errno, sent;
    /* Python resets and initializes the LCD after open. NOP parks its latch
     * safely while this short transaction selects only the touch controller. */
    if (set_mode(p, SPI_MODE_0) < 0 || lcd_frame(p, nop, sizeof(nop)) < 0 ||
        set_latch(p, true) < 0 || set_mode(p, SPI_MODE_0) < 0 ||
        set_mode(p, SPI_MODE_0 | SPI_NO_CS) < 0 || set_latch(p, false) < 0)
        goto done;
    transfer.tx_buf = (uintptr_t)powerdown;
    transfer.len = sizeof(powerdown);
    transfer.speed_hz = TOUCH_SPEED;
    transfer.bits_per_word = 8;
    sent = ioctl_retry(p->spi_fd, SPI_IOC_MESSAGE(1), &transfer);
    if (sent < 0)
        goto done;
    if ((size_t)sent != sizeof(powerdown)) {
        errno = EIO;
        goto done;
    }
    result = 0;
done:
    saved_errno = errno;
    if (idle_bus(p) < 0 && result == 0) {
        result = -1;
        saved_errno = errno;
    }
    if (result < 0)
        errno = saved_errno;
    return result;
}

void *kedei_open(uint32_t speed_hz)
{
    struct kedei_transport *p;
    uint8_t bpw = 8;
    int error, saved_errno;
    if (speed_hz == 0)
        speed_hz = 4000000u;
    if (speed_hz < 10000u || speed_hz > 32000000u) {
        errno = EINVAL;
        return NULL;
    }
    p = calloc(1, sizeof(*p));
    if (!p)
        return NULL;
    p->spi_fd = p->chip_fd = p->latch_fd = p->penirq_fd = -1;
    p->speed = speed_hz;
    error = pthread_mutex_init(&p->mutex, NULL);
    if (error) {
        free(p);
        errno = error;
        return NULL;
    }
    if (verify_spi_mapping() < 0 || verify_spi_devices() < 0)
        goto failed;
    p->spi_fd = open(SPI_DEVICE, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (p->spi_fd < 0 || flock(p->spi_fd, LOCK_EX | LOCK_NB) < 0)
        goto failed;
    if (ioctl_retry(p->spi_fd, SPI_IOC_RD_MODE32, &p->old_mode) < 0 ||
        ioctl_retry(p->spi_fd, SPI_IOC_RD_BITS_PER_WORD, &p->old_bpw) < 0 ||
        ioctl_retry(p->spi_fd, SPI_IOC_RD_MAX_SPEED_HZ, &p->old_speed) < 0)
        goto failed;
    if (find_gpio_chip(p) < 0)
        goto failed;
    p->latch_fd = request_line(p->chip_fd, 8u, GPIO_V2_LINE_FLAG_OUTPUT,
                              "wifi-meter-kedei-latch", true);
    if (p->latch_fd < 0)
        goto failed;
    p->penirq_fd = request_line(p->chip_fd, 25u,
                               GPIO_V2_LINE_FLAG_INPUT | GPIO_V2_LINE_FLAG_BIAS_PULL_UP,
                               "wifi-meter-kedei-irq", false);
    if (p->penirq_fd < 0)
        goto failed;
    p->restore_spi = true;
    if (set_mode(p, SPI_MODE_0) < 0 ||
        ioctl_retry(p->spi_fd, SPI_IOC_WR_BITS_PER_WORD, &bpw) < 0 ||
        ioctl_retry(p->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &p->speed) < 0)
        goto failed;
    if (prime_touch(p) < 0)
        goto failed;
    return p;
failed:
    saved_errno = errno;
    cleanup(p);
    pthread_mutex_destroy(&p->mutex);
    free(p);
    errno = saved_errno;
    return NULL;
}

static int unlock_result(struct kedei_transport *p, int result, int saved_errno)
{
    int error = pthread_mutex_unlock(&p->mutex);
    if (result >= 0 && error) {
        errno = error;
        return -1;
    }
    if (result < 0)
        errno = saved_errno;
    return result;
}

int kedei_write_frames(void *handle, const uint8_t *bytes, size_t frame_len,
                        size_t count)
{
    static const uint8_t nop[3] = {0x11, 0, 0};
    struct kedei_transport *p = handle;
    size_t i;
    int error, result = 0, saved_errno = 0;
    if (!p || (frame_len != 3 && frame_len != 4) ||
        (count && !bytes) || count > SIZE_MAX / frame_len) {
        errno = EINVAL;
        return -1;
    }
    error = pthread_mutex_lock(&p->mutex);
    if (error) {
        errno = error;
        return -1;
    }
    if (set_mode(p, SPI_MODE_0) < 0) {
        result = -1;
        saved_errno = errno;
    }
    for (i = 0; result == 0 && i < count; ++i) {
        if (lcd_frame(p, bytes + i * frame_len, frame_len) < 0) {
            result = -1;
            saved_errno = errno;
        }
    }
    /* A later touch assertion may relatch the last shifted LCD packet. Park
     * command/data/pixel batches at NOP. Never add a command after a four-byte
     * reset packet: that would undo reset-low before Python's hold delay. */
    if (result == 0 && count && frame_len == 3 && !p->lcd_parked &&
        lcd_frame(p, nop, sizeof(nop)) < 0) {
        result = -1;
        saved_errno = errno;
    }
    if (idle_bus(p) < 0 && result == 0) {
        result = -1;
        saved_errno = errno;
    }
    return unlock_result(p, result, saved_errno);
}

int kedei_read_touch(void *handle, int *raw_x, int *raw_y, int *raw_z1, int *raw_z2)
{
    struct kedei_transport *p = handle;
    /* Keep ADC powered between X/Y/Z conversions; last Y read powers it down
     * and reenables PENIRQ. Each conversion gets its own 24 SPI clocks. */
    static const uint8_t tx[15] = {
        0xd1, 0, 0, 0x91, 0, 0, 0xb1, 0, 0, 0xc1, 0, 0, 0x90, 0, 0
    };
    static const uint8_t nop[3] = {0x11, 0, 0};
    uint8_t rx[sizeof(tx)] = {0};
    struct spi_ioc_transfer transfer = {0};
    int error, down, sent, x, y, z1, z2;
    int result = 0, saved_errno = 0;
    if (!p || !raw_x || !raw_y || !raw_z1 || !raw_z2) {
        errno = EINVAL;
        return -1;
    }
    *raw_x = *raw_y = *raw_z1 = *raw_z2 = 0;
    error = pthread_mutex_lock(&p->mutex);
    if (error) {
        errno = error;
        return -1;
    }
    down = pen_down(p);
    if (down < 0)
        goto failed;
    if (!down)
        goto done;
    /* Park the last LCD packet at NOP before GPIO8 becomes touch CS. This
     * happens only between complete frame groups, never during a blit. */
    if (set_mode(p, SPI_MODE_0) < 0 ||
        (!p->lcd_parked && lcd_frame(p, nop, sizeof(nop)) < 0) ||
        set_latch(p, true) < 0)
        goto failed;
    /* SPI_NO_CS does not raise a CS left low. Normal-mode spi_setup explicitly
     * deselects GPIO7 before manual GPIO8 touch selection is permitted. */
    if (set_mode(p, SPI_MODE_0) < 0 || set_mode(p, SPI_MODE_0 | SPI_NO_CS) < 0 ||
        set_latch(p, false) < 0)
        goto failed;
    transfer.tx_buf = (uintptr_t)tx;
    transfer.rx_buf = (uintptr_t)rx;
    transfer.len = sizeof(tx);
    transfer.speed_hz = TOUCH_SPEED;
    transfer.bits_per_word = 8;
    sent = ioctl_retry(p->spi_fd, SPI_IOC_MESSAGE(1), &transfer);
    if (sent < 0)
        goto failed;
    if ((size_t)sent != sizeof(tx)) {
        errno = EIO;
        goto failed;
    }
    if (idle_bus(p) < 0)
        goto failed;
    down = pen_down(p);
    if (down < 0)
        goto failed;
    if (!down)
        goto done;
    x = ((((unsigned int)rx[1] << 8) | rx[2]) >> 3) & 0x0fff;
    y = ((((unsigned int)rx[4] << 8) | rx[5]) >> 3) & 0x0fff;
    z1 = ((((unsigned int)rx[7] << 8) | rx[8]) >> 3) & 0x0fff;
    z2 = ((((unsigned int)rx[10] << 8) | rx[11]) >> 3) & 0x0fff;
    if (x > 0 && x < 4095 && y > 0 && y < 4095 && z1 > 0 && z2 > z1) {
        *raw_x = x;
        *raw_y = y;
        *raw_z1 = z1;
        *raw_z2 = z2;
        result = 1;
    } else
        result = 2;
    goto done;
failed:
    result = -1;
    saved_errno = errno;
done:
    if (idle_bus(p) < 0 && result >= 0) {
        result = -1;
        saved_errno = errno;
    }
    return unlock_result(p, result, saved_errno);
}

int kedei_close(void *handle)
{
    struct kedei_transport *p = handle;
    int error, result, saved_errno;
    if (!p) {
        errno = EINVAL;
        return -1;
    }
    error = pthread_mutex_lock(&p->mutex);
    if (error) {
        errno = error;
        return -1;
    }
    result = cleanup(p);
    saved_errno = errno;
    error = pthread_mutex_unlock(&p->mutex);
    if (result == 0 && error) {
        result = -1;
        saved_errno = error;
    }
    error = pthread_mutex_destroy(&p->mutex);
    if (result == 0 && error) {
        result = -1;
        saved_errno = error;
    }
    free(p);
    if (result < 0)
        errno = saved_errno;
    return result;
}
