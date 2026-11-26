# downkeep

careful cleanup for your downloads folder.

- **report**: what is there, how old it is, what takes space, and which site files probably came from (site name only)
- **clutter**: exact copies, and zips whose every file is already extracted. nothing else is called clutter. you choose what goes to the trash, the proof is checked again right before, and one intact copy of the contents stays outside the trash
- **tidy**: sorts loose files into folders by rules you can change. you see the plan first, and one click undoes it
- **automatic**: tidies new files every hour. off until you turn it on, and it never uses the trash

it never deletes a file for good and never replaces one file with another. if something can't be done safely it is left alone, and you are told why.

## get it

the first release will be on the [releases page](https://github.com/ShaikhAman01/downkeep/releases).

- **windows**: download the zip, choose "extract all", open the folder, double-click Downkeep
- **linux** (x64, glibc 2.35 or newer): unpack, run `./downkeep`. `./install.sh` adds it to your menu, without sudo. automatic needs systemd
- **macos**: read-only for now, from source. it reports, but doesn't trash, tidy or run by itself

it isn't signed, so windows will probably say "windows protected your pc" the first time. "more info", then "run anyway" opens it. that warning means windows doesn't know who made it, so only go on if you got it from the page above. smart app control, where it is on, may block it with no way around. each release lists a sha-256 for every download.

it uses a browser you already have for its window.

on windows, undo can't take files out of the recycle bin. you can do that yourself from the recycle bin.

## privacy

nothing leaves your computer. no accounts, no tracking, no update checks. the window is served from your own machine and only answers to itself. to prove a copy it reads files of equal size, and to prove a zip is extracted it compares checksums. it keeps a history of what it moved, on your computer, so that it can undo.

## from source

```bash
pip install .
downkeep              # opens a window
downkeep report       # stays in the terminal, changes nothing
downkeep tidy         # shows the plan; add --apply to move
downkeep history
downkeep undo
downkeep auto status
```

needs python 3.12 or newer. no dependencies.
