# DualSenseHaptics 1.1.1 — HID connection never opens when the controller exposes extra HID interfaces (lightbar and adaptive triggers never work)

## Summary

The engine's SetupDi search keeps the **first** HID interface whose `HidD_GetAttributes` VID/PID is a DualSense
(`0x054C` / `0x0CE6`, `0x0DF2`). It never checks whether that interface is the Game Pad, and it does not move on
to another interface when the later read/write open fails. If the
controller exposes more than one HID interface under the same VID/PID, and Windows lists a non-openable one first
(here a keyboard), the engine retries that interface forever:

- the lightbar and adaptive triggers never work;
- haptics through the audio endpoint and the rewrite of the game's own rumble reports still work, which hides the
  problem.

## Environment

- Lies of P (Steam), UE4SS experimental `v3.0.1-1181-g83330635` (C++ mod loads: `engine in DLL`).
- DualSenseHaptics 1.1.1 (`main.dll` SHA-256 `21f692b14ec434aa4709a1d8480cfc645470397227f8be782593fe8ebc487e6b`).
- DualSense Edge connected through **DS5 Bridge** (<https://github.com/SundayMoments/DS5_Bridge>). A Raspberry Pi Pico 2 W
  pairs with the controller over Bluetooth and presents it to Windows as a USB DualSense Edge, `VID_054C&PID_0DF2`.
  The DS5 Bridge desktop app was **not** running.
- The same result with Steam Input on and off for the game, and with Steam restarted.

## What Windows sees for VID_054C&PID_0DF2

| Interface | Device | HID usage |
|---|---|---|
| MI_00 | DualSense Edge Wireless Controller (audio) | – |
| **MI_03** | HID-compliant game controller | **0001:0005 Game Pad** |
| **MI_04** | **HID Keyboard Device** (the bridge's keyboard/media actions) | **0001:0006 Keyboard** |
| MI_05 | DS5 Bridge Control (vendor companion interface) | – |

## Proof

The SetupDi HID interface list, opened with `GENERIC_READ|GENERIC_WRITE` and `FILE_SHARE_READ|FILE_SHARE_WRITE`
(the same way the engine opens it), with the game running:

```
DENIED (error 5)  usage 0001:0006  \\?\hid#vid_054c&pid_0df2&mi_04#8&22160322&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}\kbd
OPENS             usage 0001:0005  \\?\hid#vid_054c&pid_0df2&mi_03#8&35461f60&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}
```

- The keyboard interface is listed **first**. Windows refuses read/write opens of any keyboard collection.
- The Game Pad interface opens fine.

`dsengine.log` for a full session shows only the first one being tried, about every 2 s
(1,046 times in 17 minutes in one session):

```
HID: CreateFile failed (error 5); trying NtCreateFile
HID: NtCreateFile failed (0xC0000022)
```

## Where it happens (1.1.1 `main.dll`, from the disassembly)

The SetupDi search loop is at RVA `0x3C6D0`–`0x3C8F4`:

```
for each HID interface (SetupDiEnumDeviceInterfaces / SetupDiGetDeviceInterfaceDetailA):
    h = CreateFileA(path, 0, FILE_SHARE_READ|FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL)   // RVA 0x3C82A
    if HidD_GetAttributes(h) and VendorID == 0x054C and ProductID in {0x0CE6, 0x0DF2}:
        result = path
    CloseHandle(h)
    if result not empty: break            // RVA 0x3C8E8: cmp qword [rsp+68h],0 ; je 0x3C6D0
```

If SetupDi finds nothing, the CM fallback (`CM_Get_Device_Interface_ListW`, RVA `0x3C12D`) matches lowercase path
substrings (`054c` plus `pid_0ce6` / `pid_0df2` / `pid&0ce6` / `pid&0df2`). The chosen path is then opened for
read/write at RVA `0x3D2F1`. That is the open that fails with error 5, and `NtCreateFile` then fails with
`0xC0000022`.

## The fix

In the HID search, for each candidate interface whose VID/PID matches:

1. **Check the usage before opening for read/write.**
   - Open with `dwDesiredAccess = 0` (query-only, which works even for keyboards).
   - Call `HidD_GetPreparsedData` and `HidP_GetCaps`.
   - Keep the interface only if `UsagePage == 0x01 && Usage == 0x05` (Game Pad). For extra safety, also require
     the output report length you expect (64 on USB, 78 on Bluetooth).
2. **On any open failure, continue to the next candidate** instead of returning and retrying the same path on the
   next timer tick.
3. (Optional) Skip paths ending in `\kbd` / `\mouse`. Windows appends these to keyboard and mouse collections.

Sketch:

```cpp
for (const auto& path : candidates_with_pid) {            // SetupDi or CM_Get_Device_Interface_List
    HANDLE q = CreateFileA(path.c_str(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (q == INVALID_HANDLE_VALUE) continue;
    PHIDP_PREPARSED_DATA pp = nullptr; HIDP_CAPS caps{};
    bool pad = HidD_GetPreparsedData(q, &pp) && HidP_GetCaps(pp, &caps) == HIDP_STATUS_SUCCESS
               && caps.UsagePage == 0x01 && caps.Usage == 0x05;
    if (pp) HidD_FreePreparsedData(pp);
    CloseHandle(q);
    if (!pad) continue;                                    // keyboard, vendor or other collection: skip
    HANDLE h = CreateFileA(path.c_str(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
    if (h != INVALID_HANDLE_VALUE) return h;               // found it
    // log once and try the next candidate
}
```

This is harmless for a genuine DualSense / DualSense Edge, which has a single Game Pad collection. It also fixes any
adapter, bridge or virtual device that adds extra HID interfaces under the same VID/PID.

## Workaround used locally (until a fixed build)

I binary-patched `main.dll` (MIT licence). Two bytes were changed so the SetupDi loop always runs to the end and keeps
the **last** match, not the first. On this setup the keyboard is listed before the gamepad.

| RVA / file offset | Original | Patched |
|---|---|---|
| `0x3C8EE` / `0x3BCEE` | `0F 84 DC FD FF FF` (`je 0x3C6D0`) | `90 E9 DC FD FF FF` (`nop; jmp 0x3C6D0`) |

The loop still ends when `SetupDiEnumDeviceInterfaces` returns FALSE. A genuine controller has one match, so it is
unaffected.
Original SHA-256: `21f692b14ec434aa4709a1d8480cfc645470397227f8be782593fe8ebc487e6b`.
Patched SHA-256: `03a121f460cf9093f7e07095991b6b02d3cf9daafbdc4ce3c2604d7c25623bf7`.

This depends on the order Windows lists the interfaces in, so it's a stopgap. The usage check above is the real fix.

## Two smaller observations from the same sessions

1. **"Steam overlay absent" is logged even when the overlay is present.** With Steam Input on, Steam's
   `gameoverlay_renderer.txt` shows `GameOverlayRenderer` attaching to `LOP-Win64-Shipping.exe` in the same second
   that the engine logged `engine started inside the game (Steam overlay absent)`. The check seems to run before
   the overlay DLL loads; re-checking a few seconds later would be more reliable.
2. **One silent process exit while idle.** At 20:54:40 the game closed with no Unreal crash report.
   - Windows Error Reporting: `0xC0000409` in `ucrtbase.dll`, fast-fail code 7 (`abort()`).
   - `dsengine.log`'s last line was 2 s earlier: the usual HID retry, then nothing.
   - Steam reports exit code `-1073740791`.
   - It happened once and isn't proven to come from the engine, but it was the only native add-on running at the
     time. Catching exceptions on the engine's background threads, and logging them, would show the cause if it
     recurs.
