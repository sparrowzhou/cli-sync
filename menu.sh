#!/usr/bin/env bash
# Open the terminal menu and install the sftpsync shortcut.
DIR="$(cd "$(dirname "$0")" && pwd)"
SHORTCUT="${HOME}/.local/bin/sftpsync"
# Install a shortcut pointing to this copy of the menu.
install_shortcut() {
  local want
  want="$(printf '#!/bin/sh\nexec bash "%s/menu.sh" "$@"\n' "${DIR}")"
  [ -f "${SHORTCUT}" ] && [ "$(cat "${SHORTCUT}")" = "${want}" ] && return 0
  mkdir -p "$(dirname "${SHORTCUT}")" && printf '%s\n' "${want}" > "${SHORTCUT}" && chmod +x "${SHORTCUT}" || return 0
  printf '%s\n' "Installed the shortcut: type sftpsync in any folder to open this menu."
  case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *)
      # Put the shortcut directory on the shell search path.
      local rc="${HOME}/.bashrc"
      [ "$(basename "${SHELL:-}")" = "zsh" ] && rc="${HOME}/.zshrc"
      grep -q 'sftpsync' "${rc}" 2>/dev/null \
        || printf '\n# sftpsync shortcut\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "${rc}"
      printf '%s\n' "(log in again or open a new terminal window before sftpsync works)"
      ;;
  esac
  echo
}

install_shortcut
# Prepare the runtime before opening the menu.
if ! SFTP_SYNC_QUIET_SETUP=1 bash "${DIR}/sync.sh" --setup; then
  echo; printf '%s\n' "The runtime environment is not ready; press Enter to quit…"; read -r _; exit 1
fi
exec bash "${DIR}/sync.sh" --menu
