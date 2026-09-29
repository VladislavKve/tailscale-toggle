<p align="center">
  <img src="assets/app-icon.png" width="128" alt="Иконка Tailscale Toggle">
</p>

# Tailscale Toggle

Неофициальная графическая утилита GTK 4 / Libadwaita для управления Tailscale
в Ubuntu и GNOME. Она объединяет состояние подключения, exit nodes, трей,
браузерную авторизацию и безопасный вход по auth key.

> Это независимый community-проект, не связанный с Tailscale Inc. и не
> одобренный компанией.

[English README](README.md)

<p align="center">
  <img src="docs/screenshots/tailscale-toggle-overview-light.png" width="47%" alt="Экран подключения в светлой теме">
  <img src="docs/screenshots/tailscale-toggle-auth-dark.png" width="47%" alt="Экран авторизации в тёмной теме">
</p>

## Возможности

- Отдельные состояния подключения, входа, одобрения устройства и ошибок.
- Выбор прямого маршрута или exit node, поиск и online/offline-индикация.
- Вход через браузер: пароль, MFA и passkey не попадают в приложение.
- Скрытый ввод `tskey-auth-`; ключ не сохраняется и не передаётся прямо в argv.
- Возврат к предыдущему локальному профилю после незавершённого входа.
- Светлая, тёмная и системная темы, отдельный AppIndicator-процесс для трея.

## Требования

- Ubuntu 24.04 или новее с GNOME.
- [Установленный Tailscale](https://tailscale.com/docs/install/linux).

Основные зависимости:

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 policykit-1
```

Дополнительно для трея:

```bash
sudo apt install gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
  gnome-shell-extension-appindicator
```

## Установка

```bash
git clone https://github.com/VladislavKve/tailscale-toggle.git
cd tailscale-toggle
./install.sh
```

Установка выполняется без root в `~/.local/share/tailscale-toggle`. В меню GNOME
появится **Tailscale Toggle**, а в `~/.local/bin` — команда `tailscale-toggle`.
Установщик не меняет настройки Tailscale.

Обновление:

```bash
git pull --ff-only
./install.sh
```

Удаление:

```bash
~/.local/share/tailscale-toggle/uninstall.sh
```

Флаг `--purge` дополнительно удаляет пользовательские настройки из
`~/.config/tailscale-toggle`.

## Безопасность авторизации

- Логин, пароль, MFA и passkey вводятся только на странице identity provider.
- Автоматически открываются только HTTPS-ссылки хоста `login.tailscale.com`.
- Auth key хранится лишь во временном файле с правами `0600`, скрывается из
  сообщений об ошибках и удаляется при любом результате.
- Повышение прав через PolicyKit запрашивается только при отказе локального CLI.
- Перед добавлением другого аккаунта приложение предупреждает о немедленной
  смене профиля и позволяет вернуться к предыдущему.

Уязвимости следует отправлять через
[приватный GitHub Security Advisory](https://github.com/VladislavKve/tailscale-toggle/security/advisories/new),
а не через публичный issue.

## Разработка

```bash
./run.sh --preview connected --no-tray
/usr/bin/python3 -B -m unittest discover -s tests -v
```

Подробности — в [CONTRIBUTING.md](CONTRIBUTING.md). Лицензия указана в
[LICENSE](LICENSE), информация о товарных знаках и ресурсах — в
[NOTICE.md](NOTICE.md).
