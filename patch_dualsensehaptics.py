r"""Patch DualSenseHaptics 1.1.1 dlls\main.dll (MIT licence, jeffloire) so its HID search picks the LAST interface
that matches the DualSense VID/PID instead of the FIRST.

Why: with a DS5 Bridge (Raspberry Pi Pico 2 W USB gadget) the PC sees VID_054C&PID_0DF2 with a keyboard interface
(MI_04, usage 0001:0006) listed BEFORE the gamepad interface (MI_03, usage 0001:0005). The engine's SetupDi search
(code at RVA 0x3C6D0-0x3C8F4) opens each HID interface query-only, keeps the first whose HidD_GetAttributes VID/PID
match, and stops. It then opens that path for read/write, which Windows refuses for keyboards (error 5 /
0xC0000022), forever: no lightbar, no adaptive triggers.

The patch changes the loop's back edge at RVA 0x3C8EE (file offset 0x3BCEE)
    0F 84 DC FD FF FF      je  0x3C6D0      (loop again only while nothing has matched)
to
    90 E9 DC FD FF FF      nop; jmp 0x3C6D0 (always loop; the loop still ends when SetupDiEnumDeviceInterfaces fails)
so every interface is checked and the last match is kept. A genuine controller has one match, so it is unaffected;
with the bridge the gamepad (listed after the keyboard) is chosen.

Usage:  python patch_dualsensehaptics.py <path to main.dll>          patch (keeps main.dll.orig next to it)
        python patch_dualsensehaptics.py <path to main.dll> --undo   restore main.dll.orig
"""
import hashlib
import os
import shutil
import sys

ORIG_SHA256 = "21f692b14ec434aa4709a1d8480cfc645470397227f8be782593fe8ebc487e6b"   # DualSenseHaptics 1.1.1 main.dll
OFFSET = 0x3BCEE
OLD = bytes.fromhex("0f84dcfdffff")
NEW = bytes.fromhex("90e9dcfdffff")


def sha(b):
    return hashlib.sha256(b).hexdigest()


dll = sys.argv[1]
orig = dll + ".orig"
if "--undo" in sys.argv:
    shutil.copy2(orig, dll)
    print("restored", dll, "sha256", sha(open(dll, "rb").read()))
    sys.exit(0)

if not os.path.exists(orig):
    shutil.copy2(dll, orig)
src = open(orig, "rb").read()
if sha(src) != ORIG_SHA256:
    sys.exit(f"{orig} is not the DualSenseHaptics 1.1.1 main.dll this patch was written for (sha256 {sha(src)})")
assert src[OFFSET:OFFSET + len(OLD)] == OLD, "unexpected bytes at the patch offset"
out = src[:OFFSET] + NEW + src[OFFSET + len(NEW):]
open(dll, "wb").write(out)
diff = [i for i in range(len(src)) if src[i] != out[i]]
print("patched from the original: %d byte(s) changed at file offset(s) %s" % (len(diff), ", ".join(hex(i) for i in diff)))
print("patched sha256", sha(out))
