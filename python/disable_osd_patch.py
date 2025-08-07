#!/usr/bin/env python3
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print("Usage: disable_osd_patch.py <firmware.bin>")
    sys.exit(1)

firmware_path = Path(sys.argv[1])
data = bytearray(firmware_path.read_bytes())

# Create a copy for emulator with reset vector stub (LJMP 0x0300)
# so mcs51emu-plus can start execution at the real reset handler.
# This patch is only needed for running in the emulator.
emu_data = bytearray(data)
emu_data[0:3] = b"\x02\x03\x00"  # LJMP 0x0300
emu_path = firmware_path.with_name(firmware_path.stem + "_emul.bin")
emu_path.write_bytes(emu_data)

# Disable OSD overlay by forcing 0x0B76 to zero and skipping
# the code that sets bit1 of that register at reset.
patched = bytearray(data)
# Replace instructions at offset 0x558-0x55f:
#   MOV A,#0x81
#   LCALL 0xAA03
#   ORL A,#0x02
#   MOVX @DPTR,A
# with:
#   MOV A,#0x00
#   MOVX @DPTR,A
#   NOP x4 (to preserve size)
patch = b"\x74\x00\xf0\x00\x00\x00\x00\x00"
start = 0x558
patched[start:start+len(patch)] = patch
no_osd_path = firmware_path.with_name(firmware_path.stem + "_no_osd.bin")
no_osd_path.write_bytes(patched)

print(f"Wrote emulator image to {emu_path}")
print(f"Wrote patched image without OSD to {no_osd_path}")
