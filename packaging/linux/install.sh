#!/usr/bin/env bash
# Copies Downkeep into your own ~/.local. No sudo, no questions, and it never replaces a file it didn't put there.
set -u
umask 022

PARTS=(downkeep _internal licenses LICENSE.txt "Read me first.txt" downkeep.desktop downkeep.png install.sh uninstall.sh)
LIST=installed-files.txt

stop() {
    printf 'downkeep: %s\n' "$1" >&2
    exit 1
}

hash_of() {
    local line
    line=$(sha256sum < "$1") || return 1
    printf '%s' "${line%% *}"
}

# recorded LIST PATH: the checksum written down for this path, if any
recorded() {
    local line
    [ -f "$1" ] || return 1
    while IFS= read -r line; do
        if [ "${line:66}" = "$2" ]; then
            printf '%s' "${line:0:64}"
            return 0
        fi
    done < "$1"
    return 1
}

# ours LIST PATH FILE: a plain file that is exactly as Downkeep wrote it
ours() {
    local want
    [ -f "$3" ] && [ ! -L "$3" ] && want=$(recorded "$1" "$2") && [ "$want" = "$(hash_of "$3")" ]
}

is_open() {
    local process
    for process in /proc/[0-9]*/exe; do
        [ "$process" -ef "$app/downkeep" ] && return 0
    done
    return 1
}

# Removes only files named in the list that were not changed since, then the folders left empty
remove_listed() {
    local folder=$1 list=$2 checked=$3 line path
    while IFS= read -r line; do
        path=${line:66}
        case "/$path/" in //* | */../*) continue ;; esac
        if [ "$checked" = no ] || ours "$list" "$path" "$folder/$path"; then
            rm -f -- "$folder/$path"
        fi
    done < "$list"
    [ "$list" = "$folder/$LIST" ] && rm -f -- "$list"
    find "$folder" -depth -type d -empty -delete
}

# The first thing in the program folder that Downkeep didn't put there
foreign_in() {
    local file
    while IFS= read -r -d '' file; do
        [ "$file" = "$app/$LIST" ] && continue
        if ! ours "$app/$LIST" "${file#"$app"/}" "$file"; then
            printf '%s' "$file"
            return
        fi
    done < <(find "$app" ! -type d -print0)
}

write_new() {
    (
        set -o noclobber
        cat -- "$1" > "$2"
    )
}

place_shared() {
    mkdir -p -- "$(dirname -- "$2")" || return 1
    if [ -e "$2" ] || [ -L "$2" ]; then
        ours "$3" "$2" "$2" || [ "$(hash_of "$1")" = "$(hash_of "$2")" ] || return 1
        rm -f -- "$2" || return 1
    fi
    write_new "$1" "$2"
}

# Two layers of escaping: the Exec line's own quotes, then the desktop file's
exec_line() {
    local text=$1
    text=${text//\\/\\\\}
    text=${text//\"/\\\"}
    text=${text//\`/\\\`}
    text=${text//\$/\\\$}
    text=${text//%/%%}
    text=${text//\\/\\\\}
    printf 'Exec="%s"\n' "$text"
}

abandon() {
    [ -n "${old:-}" ] && [ ! -e "$app" ] && mv -T -- "$old" "$app"
    [ -n "${old:-}" ] && [ -d "$old" ] && find "$old" -maxdepth 0 -empty -delete
    [ -n "${stage:-}" ] && [ -f "$stage/$LIST" ] && remove_listed "$stage" "$stage/$LIST" no
    stage=
}

check_shared() {
    [ -e "$2" ] || [ -L "$2" ] || return 0
    ours "$app/$LIST" "$2" "$2" && return 0
    [ -f "$2" ] && [ ! -L "$2" ] && [ "$(hash_of "$1")" = "$(hash_of "$2")" ] && return 0
    stop "$2 is already there and Downkeep didn't put it there. Nothing was changed."
}

install_it() {
    local part file line
    old=
    for part in "${PARTS[@]}"; do
        [ -e "$here/$part" ] || stop "$part is missing next to install.sh. Unpack the whole download again."
    done
    [ "$here" -ef "$app" ] && stop "this is the installed copy. Run install.sh from a newly unpacked download."
    if [ -e "$app" ] || [ -L "$app" ]; then
        [ -d "$app" ] && [ ! -L "$app" ] && [ -f "$app/$LIST" ] ||
            stop "$app is already there and Downkeep didn't put it there. Nothing was changed."
        is_open && stop "Downkeep is open. Close it, then run install.sh again. Nothing was changed."
        file=$(foreign_in)
        [ -z "$file" ] || stop "$file was added or changed since Downkeep was installed. Nothing was changed."
    fi

    mkdir -p -- "$lib" || stop "couldn't create $lib"
    stage=$(mktemp -d -- "$lib/.downkeep-new.XXXXXX") || stop "couldn't create a folder in $lib"
    trap abandon EXIT
    trap 'exit 130' INT TERM HUP
    chmod 755 -- "$stage"
    : > "$stage/$LIST"
    for part in "${PARTS[@]}"; do
        while IFS= read -r -d '' file; do
            printf '%s  %s\n' "$(hash_of "$file")" "${file#"$here"/}" >> "$stage/$LIST" || stop "couldn't write to $lib"
        done < <(find "$here/$part" -type f -print0)
    done
    for part in "${PARTS[@]}"; do
        cp -R -- "$here/$part" "$stage/$part" || stop "couldn't copy the files. Nothing was installed."
    done
    entry_file=$stage/downkeep.desktop
    while IFS= read -r line; do
        case $line in
            Exec=*) exec_line "$app/downkeep" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$here/downkeep.desktop" > "$entry_file" || stop "couldn't write to $lib"
    grep -v '  downkeep\.desktop$' "$stage/$LIST" > "$stage/$LIST.part" && cat "$stage/$LIST.part" > "$stage/$LIST"
    rm -f -- "$stage/$LIST.part"
    printf '%s  %s\n' "$(hash_of "$entry_file")" downkeep.desktop >> "$stage/$LIST"

    check_shared "$entry_file" "$entry"
    check_shared "$stage/downkeep.png" "$icon"
    printf '%s  %s\n' "$(hash_of "$entry_file")" "$entry" >> "$stage/$LIST"
    printf '%s  %s\n' "$(hash_of "$stage/downkeep.png")" "$icon" >> "$stage/$LIST"

    if [ -d "$app" ]; then
        old=$(mktemp -d -- "$lib/.downkeep-old.XXXXXX") && mv -T -- "$app" "$old" ||
            stop "couldn't move the old copy aside. Nothing was changed."
    fi
    if ! mv -T -- "$stage" "$app"; then
        stop "couldn't put the new copy in place. Nothing was changed."
    fi
    stage=
    place_shared "$app/downkeep.desktop" "$entry" "${old:-$app}/$LIST" &&
        place_shared "$app/downkeep.png" "$icon" "${old:-$app}/$LIST" ||
        stop "Downkeep is in $app, but the menu entry couldn't be written."
    [ -z "$old" ] || remove_listed "$old" "$old/$LIST" yes
    printf 'Downkeep is installed. Open it from your menu, or run:\n  %s\n' "$app/downkeep"
}

# Only the hourly check that starts this copy. The part of its path matched looks the same however it is quoted.
remove_schedule() {
    local wants=$units/timers.target.wants/downkeep.timer
    grep -qsF '/.local/lib/downkeep/downkeep' -- "$units/downkeep.service" || return 0
    if command -v systemctl > /dev/null; then
        systemctl --user disable --now downkeep.timer > /dev/null 2>&1
    fi
    rm -f -- "$units/downkeep.timer" "$units/downkeep.service" || stop "couldn't turn Automatic off"
    [ ! -L "$wants" ] || rm -f -- "$wants"
    if command -v systemctl > /dev/null; then
        systemctl --user daemon-reload > /dev/null 2>&1
    fi
    printf 'Automatic tidying is off.\n'
}

remove_it() {
    local path left=
    [ -d "$app" ] && [ ! -L "$app" ] && [ -f "$app/$LIST" ] || stop "Downkeep isn't installed in $app"
    is_open && stop "Downkeep is open. Close it, then run uninstall.sh again. Nothing was changed."
    remove_schedule
    for path in "$entry" "$icon"; do
        [ -e "$path" ] || [ -L "$path" ] || continue
        if ours "$app/$LIST" "$path" "$path"; then
            rm -f -- "$path"
        else
            left=$path
        fi
    done
    remove_listed "$app" "$app/$LIST" yes
    [ -e "$app" ] && left=$app
    if [ -n "$left" ]; then
        printf 'Downkeep is removed. Left alone because it was added or changed since: %s\n' "$left"
    else
        printf 'Downkeep is removed.\n'
    fi
    printf 'Your files were not touched.\n'
    [ ! -d "$state" ] || printf 'The history of what it moved is kept in %s\n' "$state"
}

main() {
    [ "$(id -u)" -ne 0 ] || stop "run this as yourself, without sudo"
    case ${HOME:-} in
        *$'\n'* | *=*) stop "your home folder's name can't be used in a menu entry" ;;
        /*) ;;
        *) stop "HOME isn't set" ;;
    esac
    [ -d "$HOME" ] || stop "$HOME isn't there"
    data=$HOME/.local/share
    case ${XDG_DATA_HOME:-} in *$'\n'*) ;; /*) data=$XDG_DATA_HOME ;; esac
    config=$HOME/.config
    case ${XDG_CONFIG_HOME:-} in /*) config=$XDG_CONFIG_HOME ;; esac
    state=$HOME/.local/state/downkeep
    case ${XDG_STATE_HOME:-} in /*) state=$XDG_STATE_HOME/downkeep ;; esac
    lib=$HOME/.local/lib
    app=$lib/downkeep
    entry=$data/applications/downkeep.desktop
    icon=$data/icons/hicolor/512x512/apps/downkeep.png
    units=$config/systemd/user
    here=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || stop "couldn't find the folder this is in"
    case ${1:-} in
        "") install_it ;;
        --remove) remove_it ;;
        *) stop "run install.sh or uninstall.sh without anything after it" ;;
    esac
}

main "$@"
exit
