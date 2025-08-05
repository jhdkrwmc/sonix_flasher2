/* Build with:  gcc -std=c11 -Wall -Wextra -O2 \
 *     sonix_burn.exe.c -o snx_flash.exe \
 *     -I/mingw64/include/libusb-1.0 -L/mingw64/lib -lusb-1.0
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libusb-1.0/libusb.h>
#include "sonix_burn.exe.h"

/* ---- Helper functions (unchanged from your original) -------------- */
int snx_xu_set(libusb_device_handle *handle, unsigned char vc_if,
               unsigned char xu_id, unsigned char cs,
               const unsigned char *payload, unsigned short len)
{
    unsigned short wValue = (cs << 8);
    unsigned short wIndex = (xu_id << 8) | vc_if;
    return libusb_control_transfer(handle, 0x21, 0x01,
                                   wValue, wIndex,
                                   (unsigned char *)payload,
                                   len, 3000);
}

int snx_xu_get(libusb_device_handle *handle, unsigned char vc_if,
               unsigned char xu_id, unsigned char cs,
               unsigned char *out, unsigned short len)
{
    unsigned short wValue = (cs << 8);
    unsigned short wIndex = (xu_id << 8) | vc_if;
    return libusb_control_transfer(handle, 0xA1, 0x81,
                                   wValue, wIndex, out,
                                   len, 3000);
}

int snx_sf_read(snx_device *dev, unsigned int addr,
                unsigned int length, unsigned char *out)
{
    unsigned int remaining = length;
    unsigned char *p = out;
    while (remaining) {
        unsigned short chunk = remaining > 1023 ? 1023 : remaining;
        unsigned char payload[5] = {
            (addr >> 16) & 0xFF,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            (chunk >> 8) & 0xFF,
            chunk & 0xFF
        };
        int r = snx_xu_set(dev->handle, dev->vc_interface,
                           dev->xu_id, XU_CMD_SPI_READ_SET,
                           payload, sizeof(payload));
        if (r < 0) return r;
        r = snx_xu_get(dev->handle, dev->vc_interface,
                       dev->xu_id, XU_CMD_SPI_READ_GET,
                       p, chunk);
        if (r < 0 || r != chunk) return r < 0 ? r : -1;
        addr += chunk;
        p += chunk;
        remaining -= chunk;
    }
    return 0;
}
/* ------------------------------------------------------------------ */
/*  Minimal device-lifecycle helpers                                  */
/* ------------------------------------------------------------------ */
int snx_open(snx_device *dev,
             unsigned short vid, unsigned short pid,
             unsigned char vc_if, unsigned char xu_id)
{
    memset(dev, 0, sizeof(*dev));

    if (libusb_init(&dev->ctx) != 0)
        return -1;

    dev->handle = libusb_open_device_with_vid_pid(dev->ctx, vid, pid);
    if (!dev->handle) {
        libusb_exit(dev->ctx);
        return -1;
    }

    dev->vc_interface = vc_if;
    dev->xu_id        = xu_id;
    /* Optional: claim the VC interface (not always needed on Windows) */
    libusb_claim_interface(dev->handle, vc_if);
    return 0;
}

void snx_close(snx_device *dev)
{
    if (dev->handle) {
        libusb_release_interface(dev->handle, dev->vc_interface);
        libusb_close(dev->handle);
    }
    if (dev->ctx)
        libusb_exit(dev->ctx);
    memset(dev, 0, sizeof(*dev));
}

int snx_dump_firmware(snx_device *dev,
                      unsigned int addr, unsigned int length,
                      const char *path)
{
    unsigned char *buf = (unsigned char *)malloc(length);
    if (!buf) return -1;

    int r = snx_sf_read(dev, addr, length, buf);
    if (r == 0) {
        FILE *f = fopen(path, "wb");
        if (f) {
            fwrite(buf, 1, length, f);
            fclose(f);
        } else {
            r = -1;
        }
    }
    free(buf);
    return r;
}
/* ------------------------------------------------------------------ */

/* ---- Minimal CLI entry point -------------------------------------- */
int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "Usage: %s dump|read\n", argv[0]);
        return 1;
    }

    snx_device dev = {0};
    if (snx_open(&dev, 0x0C45, 0x6366, 0, 3) != 0) {
        fprintf(stderr, "Device open failed\n");
        return 1;
    }

    if (strcmp(argv[1], "dump") == 0) {
        if (snx_dump_firmware(&dev, 0, 0x20000, "firmware_dump.bin") == 0)
            puts("Firmware dumped to firmware_dump.bin");
        else
            fprintf(stderr, "Dump failed\n");
    } else if (strcmp(argv[1], "read") == 0) {
        unsigned char buf[256];
        if (snx_sf_read(&dev, 0, sizeof(buf), buf) == 0)
            puts("Read 256 bytes OK");
        else
            fprintf(stderr, "Read failed\n");
    } else {
        fprintf(stderr, "Unknown command\n");
    }

    snx_close(&dev);
    return 0;
}