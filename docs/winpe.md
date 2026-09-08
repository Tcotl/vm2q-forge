# VM2Q Forge WinPE/DISM helper

`winpe-amd64.iso` is a prepared WinPE image, not a normal Windows
installation ISO. Its `sources/boot.wim` must contain
`assets/winpe/startnet.cmd` as
`/Windows/System32/startnet.cmd`.

Build it once on a machine with `wimlib-imagex` and `xorriso`:

```bash
python packaging/build_winpe_iso.py \
  /path/to/WinPE_amd64.iso \
  /path/to/winpe-amd64.iso \
  --arch amd64
```

The builder updates every image in `boot.wim`, validates the WIM architecture,
preserves the source ISO boot metadata, and adds an architecture marker. A
Windows 11 ARM64 ISO must not be used for an amd64 Windows 7 guest.

The conversion runtime only needs the prepared ISO, a matching
`virtio-win-0.1.173-9.iso`, and the bundled `qemu-system-x86_64`. It boots
WinPE with:

1. the converted Windows disk as an IDE disk;
2. a small writable FAT16 result disk;
3. the prepared WinPE ISO;
4. the VirtIO driver ISO.

`startnet.cmd` locates the Windows partition and matching VirtIO driver
directories, runs DISM for `viostor`, optional `vioscsi`, `NetKVM`, and
`vioserial`, copies the matching QEMU Guest Agent MSI, and registers the
generated installer script. In the default bake mode, the Python process then
boots the serviced Windows disk once with a temporary virtio-serial channel.
The QGA MSI is installed, the `QEMU-GA` service is explicitly configured for
automatic start (`Start=0x2`), and the temporary autologon values are removed
before `qga_installed=1` and `qga_service_start=auto` are written to
`VM2QRES.TXT`; only then is the qcow2 published.
`firstboot` mode keeps the deferred RunOnce behavior for compatibility.
