# downkeep

careful cleanup for your downloads folder.

right now it only reports. it never moves, changes or deletes a file.

- what is there, how old it is, and what takes space
- which site files probably came from (site name only)
- proof of clutter: exact copies, and zips whose every file is already extracted

```bash
pip install .
downkeep          # opens a window
downkeep report   # stays in the terminal
```

to prove a copy it reads files of equal size, and to prove a zip is extracted it compares checksums. nothing leaves your computer: the window is served from your own machine and only answers to itself. `report --quick` skips all of that.

needs python 3.12 or newer. no dependencies. linux, macos and windows.
