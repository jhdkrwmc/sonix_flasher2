#pragma once

/* Keep Win32 lean */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <libusb-1.0/libusb.h>

/* ---- Sonix‑specific Extension‑Unit command IDs -------------------- */
#define XU_CMD_SPI_READ_SET   0x23
#define XU_CMD_SPI_READ_GET   0x24
#define XU_CMD_SPI_WRITE_SET  0x25
#define XU_CMD_SPI_WRITE_DATA 0x26

/* ---- Minimal device context -------------------------------------- */
typedef struct snx_device {
    libusb_context        *ctx;
    libusb_device_handle  *handle;
    unsigned char          vc_interface;  /* VideoControl interface #  */
    unsigned char          xu_id;         /* Extension‑Unit ID         */
} snx_device;

/* ---- Optional GUI control IDs (only used if you keep the MFC bits) */
#define IDC_BTN_READ_FW  1100
#define IDC_BTN_DUMP_FW  1101

/* ---- Helper API --------------------------------------------------- */
int  snx_xu_set   (libusb_device_handle *, unsigned char, unsigned char,
                   unsigned char, const unsigned char *, unsigned short);
int  snx_xu_get   (libusb_device_handle *, unsigned char, unsigned char,
                   unsigned char, unsigned char *, unsigned short);
int  snx_sf_read  (snx_device *, unsigned int, unsigned int, unsigned char *);
int  snx_sf_write (snx_device *, unsigned int, unsigned int, const unsigned char *);
int  snx_dump_firmware(snx_device *, unsigned int, unsigned int, const char *);
int  snx_open     (snx_device *, unsigned short, unsigned short,
                   unsigned char, unsigned char);
void snx_close    (snx_device *);

/* (GUI hooks — comment out if building CLI only) */
struct CDialog;
void init_fw_buttons(struct CDialog *dlg);
void on_read_fw(HWND hwnd);
void on_dump_fw(HWND hwnd);