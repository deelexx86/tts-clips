# Установка и обслуживание TTS Clips на RHEL 9

## Что нужно на сервере

- RHEL **9.6 или новее**, x86_64, с подключёнными BaseOS и AppStream. Python 3.12 устанавливается из AppStream вместе с RPM. Для сборки и проверки использовалась AlmaLinux 9.9. Статус проверки на RHEL указан в [отчёте C1.10](C1.10_Release_Candidate.md). [Цикл поддержки AppStream](https://access.redhat.com/support/policy/updates/rhel-app-streams-life-cycle)
- NVIDIA GPU и поддерживаемая для него ветка драйвера **570.124.06 или новее**. `nvidia-smi` должен показывать GPU и поддержку CUDA 12.8 или выше. Выберите драйвер по модели карты и настройкам Secure Boot по инструкции NVIDIA. Библиотеки CUDA 12.8 поставляются с PyTorch, отдельного CUDA Toolkit для приложения не требуется. Для планирования начните с карты с 16 ГиБ VRAM и 25 ГиБ свободного места под окружение, временные загрузки и модели. Аудио и резервные копии требуют отдельного запаса. [Установка драйвера NVIDIA на RHEL 9](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/latest/red-hat-enterprise-linux.html), [версии драйверов для CUDA](https://docs.nvidia.com/cuda/archive/12.9.2/cuda-toolkit-release-notes/index.html)
- Временный исходящий HTTPS при настройке к PyPI и файловому CDN PyPI, PyTorch (`download-r2.pytorch.org`), GitHub (`github.com`) и Hugging Face с его CDN для весов. Корпоративный HTTPS-прокси и доверенные CA настройте до запуска установки. После загрузки синтез и распознавание используют локальные модели. Node.js нужен только для сборки RPM.
- Корпоративное DNS-имя, TLS-сертификат и ключ. FastAPI слушает `127.0.0.1:8765`. Пользователи работают через NGINX на 443. Группа монтажёров имеет общие права на все клипы, голоса и файлы.

## Первая установка

1. Скачайте RPM и файл `.sha256` из prerelease `v0.1.0-rc1`. Сверьте контрольную сумму. Установите пакет, NGINX и FFmpeg с `libmp3lame`. Пример с FFmpeg из одобренного корпоративного репозитория:

   ```bash
   sha256sum -c tts-clips-0.1.0-rc1-1.el9.x86_64.rpm.sha256
   sudo dnf module install nginx:1.26/common
   sudo dnf install ./tts-clips-0.1.0-rc1-1.el9.x86_64.rpm ffmpeg httpd-tools
   sudo tts-clips-setup
   ```

   Если у организации есть утверждённая сборка FFmpeg вне RPM, установите её в `/usr/local/bin/ffmpeg` и уберите `ffmpeg` из команды `dnf`. Если согласовано использование RPM Fusion Free, на зарегистрированном RHEL включите CRB и добавьте EPEL и RPM Fusion, сверяя ключи по документации этих репозиториев. Затем установите `ffmpeg` указанной выше командой. Приложение проверяет кодирование MP3 при настройке. [Инструкции EPEL](https://docs.fedoraproject.org/en-US/epel/getting-started/), [пакеты RPM Fusion Free для EL9](https://download1.rpmfusion.org/free/el/)

   ```bash
   sudo subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms
   sudo dnf install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm
   sudo dnf install https://download1.rpmfusion.org/free/el/rpmfusion-free-release-9.noarch.rpm
   ```

   `tts-clips-setup` проверяет драйвер и FFmpeg от имени службы, ставит Python-пакеты по lock-файлам, выполняет небольшую операцию на CUDA, скачивает два фиксированных снимка моделей и сверяет SHA-256. В конце он включает и запускает `tts-clips.service`. Успешно загруженные файлы используются при повторном запуске. `sudo tts-clips-setup --check` отдельно проверяет драйвер и FFmpeg.

2. Подключите корпоративный HTTPS. Скопируйте шаблон и подставьте своё имя в `server_name` и в строке с `https://clips.example.com`. Укажите пути сертификата и ключа. Создайте файл паролей при первой настройке. Для следующих учётных записей запускайте `htpasswd -B` без `-c`.

   ```bash
   sudo install -m 0644 /usr/share/tts-clips/nginx.conf.example /etc/nginx/conf.d/tts-clips.conf
   sudoedit /etc/nginx/conf.d/tts-clips.conf
   sudo htpasswd -cB /etc/nginx/tts-clips.htpasswd editor1
   sudo chown root:nginx /etc/nginx/tts-clips.htpasswd
   sudo chmod 0640 /etc/nginx/tts-clips.htpasswd
   sudo restorecon -v /etc/nginx/conf.d/tts-clips.conf /etc/nginx/tts-clips.htpasswd
   sudo setsebool -P httpd_can_network_connect on
   sudo nginx -t
   sudo systemctl enable --now nginx
   sudo systemctl reload nginx
   ```

   Разрешите входящий HTTPS только из корпоративной сети в нужной зоне firewalld или на сетевом экране. Если корпоративный прокси уже обеспечивает TLS и вход, используйте его корпоративные правила доступа и перенесите параметры из шаблона NGINX. Сохраните то же публичное HTTPS-имя для интерфейса, API и аудио. Backend должен оставаться доступным лишь с этого хоста. [NGINX и SELinux на RHEL 9](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/deploying_web_servers_and_reverse_proxies/index)

3. Проверьте `systemctl is-enabled tts-clips nginx` и `systemctl is-active tts-clips nginx`. В браузере откройте корпоративный адрес. Убедитесь, что без входа адрес требует учётные данные и после входа доступны редактор и аудио.

## Приёмка на сервере

Создайте тестовый клип и голос. Синтезируйте короткую русскую реплику, дождитесь WAV и отчёта Whisper. При замечании прослушайте дубль и примите его вручную либо соберите черновик. Скачайте MP3 и WAV, проверьте воспроизведение. Во время синтеза можно проверить загрузку GPU командой `nvidia-smi`. Затем выполните `sudo systemctl restart tts-clips`, откройте клип и проверьте исходный текст, выбранный дубль, отчёт и скачивание. После перезагрузки сервера повторите проверку адреса и статусов обеих служб.

## Данные и обслуживание

| Что | Путь |
| --- | --- |
| SQLite, WAV и выпуски | `/var/lib/tts-clips/` (`clips.sqlite3`, `clips.audio`, `clips.exports`) |
| Версионированный Python runtime и снимки моделей | `/var/lib/tts-clips-runtime/` |
| Приложение и интерфейс | `/usr/share/tts-clips/` |
| Локальные настройки | `/etc/tts-clips/tts-clips.env`, конфигурация и учётные записи NGINX |
| Журнал | `journalctl -u tts-clips -n 100`, `journalctl -u nginx -n 100` |

- **Резервная копия.** В окно обслуживания остановите `tts-clips`, сохраните весь `/var/lib/tts-clips` одним архивом и снова запустите службу. Храните копию вне сервера вместе с закрытой копией конфигурации. Для восстановления остановите службу, сохраните прежний каталог отдельно, разверните архив на его место, проверьте владельца `tts-clips:tts-clips`, выполните `restorecon -RF /var/lib/tts-clips` и запустите службу. SQLite и каталоги аудио должны быть из одной копии. Так же переносится существующая база Windows вместе с соседними каталогами `.audio` и `.exports` при остановленном исходном приложении.
- **Обновление.** Сделайте копию и оставьте службу остановленной. Установите новый RPM через `dnf install ./новый.rpm`, затем выполните `sudo tts-clips-setup` и повторите приёмку. При откате остановите службу, установите предыдущий RPM через `dnf downgrade` и при несовместимости схемы восстановите сделанную до обновления копию. Прежние модели и окружения можно сохранить до окончания проверки.
- **GPU или кодек.** Начните с `sudo tts-clips-setup --check` и `sudo journalctl -u tts-clips -n 100`. Если драйвер требует доступа к устройствам через группу, добавьте `tts-clips` в нужную системную группу и перезапустите службу. Если FFmpeg найден, а MP3 падает, проверьте доступ к `libmp3lame` командой `ffmpeg -hide_banner -encoders | grep libmp3lame`. При исчерпании диска проверьте данные, копии, версии окружений и временные загрузки.
- **Удаление.** `sudo dnf remove tts-clips` останавливает и удаляет службу и файлы программы. Каталоги данных и моделей остаются на диске. Удалите или отключите соответствующий виртуальный хост NGINX, если сервис выводится из эксплуатации.
