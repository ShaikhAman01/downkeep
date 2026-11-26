#!/usr/bin/env bash
here=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit 1
exec bash "$here/install.sh" --remove
