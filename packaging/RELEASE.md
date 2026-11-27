# Downkeep 0.1.1

Careful cleanup for your Downloads folder. Free, no account, nothing leaves your computer.

**New in 0.1.1**

- When you turn Automatic on, you can now choose which folders take new files, and when, before confirming. Before, there was no way to change them until it was already on.

**Download**

- Windows 10 or 11 (64-bit): `Downkeep-0.1.1-windows-x64.zip`
- Linux (64-bit, glibc 2.35 or newer, for example Ubuntu 22.04 or later): `downkeep-0.1.1-linux-x64.tar.gz`

It uses Chrome, Edge, Brave, Firefox or another browser you already have for its window.

**On Windows**

1. Right-click the zip and choose "Extract All". Opening it from inside the zip does not work.
2. Move the Downkeep folder somewhere it can stay, then double-click Downkeep.
3. Windows will probably say "Windows protected your PC". Downkeep is free and not signed with a paid certificate, so Windows cannot tell who made it. If you downloaded it from this page and want to go on, choose "More info", then "Run anyway". Where Smart App Control is on, it may be blocked completely; please do not turn that off for this.

If your antivirus says it found something, do not go on, and please open an issue.

**On Linux**

Unpack it and run `./downkeep`. To add it to your menu, run `./install.sh` in the unpacked folder. It copies into `~/.local`, needs no sudo and never replaces a file it did not put there. `~/.local/lib/downkeep/uninstall.sh` removes it again and turns Automatic off.

**What it does**

- Report: what is in the folder, how old it is, what takes space.
- Clutter: exact copies, and zips whose every file is already extracted. You choose what goes to the Trash. The proof is checked again right before.
- Tidy: sorts loose files into folders. You see the plan first and can undo it.
- Automatic: tidies new files every hour. Off until you turn it on. Never uses the Trash.

**What it never does**

It never deletes a file for good, never replaces one file with another, and never sends anything anywhere.

**Good to know**

- On Windows, undo cannot take files out of the Recycle Bin. You can do that yourself from the Recycle Bin.
- To remove it on Windows: turn Automatic off in Downkeep first if you use it, then close Downkeep and delete its folder.
- macOS: no download yet. From source it is read-only.
- This is an early release. If something surprises you, please open an issue.

**SHA-256**

```
(paste SHA256SUMS-windows.txt and SHA256SUMS-linux.txt from the build)
```

The downloads are built by GitHub Actions from the tagged source, with pinned tools: see `.github/workflows/build.yml`.
