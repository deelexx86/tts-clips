# Руководство разработчика TTS Clips

## Запуск из исходников

Используйте Python 3.12 для совпадения с RHEL и Node.js 22 для сборки интерфейса. Для проверки экспорта нужен FFmpeg с `libmp3lame` в `PATH`.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm ci
npm run build
TTS_CLIPS_DB="$PWD/output/dev/clips.sqlite3" .venv/bin/python -m uvicorn clips.main:create_app --factory --host 127.0.0.1 --port 8765
```

На Windows можно использовать проверенный Python 3.11. Создайте окружение командой `py -3.11 -m venv .venv`, вызывайте `.\.venv\Scripts\python.exe` и задайте путь к базе через `$env:TTS_CLIPS_DB`. Приложение открывается на `http://127.0.0.1:8765`. Редактор работает сразу. Чтобы включить синтез и QC, перед запуском укажите абсолютные пути:

| Переменная | Значение |
| --- | --- |
| `TTS_CLIPS_VOX_PYTHON`, `TTS_CLIPS_VOX_MODEL` | Python с CUDA-версией PyTorch и VoxCPM, каталог проверенных весов |
| `TTS_CLIPS_WHISPER_PYTHON`, `TTS_CLIPS_WHISPER_MODEL` | Python с `faster-whisper`, каталог Whisper Small CTranslate2 |
| `TTS_CLIPS_VOX_SSH` | Для прежней Windows-схемы, например `gpu-tts`. При локальном запуске оставьте переменную пустой |

На Windows с `TTS_CLIPS_VOX_SSH` пути Vox относятся к Windows-хосту с GPU. Локальные Linux-пути передаются воркеру напрямую. Состав весов, ревизии и хеши заданы в `packaging/models.json`. Рядом с VoxCPM2 воркер ожидает `tts-books-model.json` с `repository` и `revision` из манифеста. На EL9 путь к установленному Python и моделям можно взять из `systemctl cat tts-clips`. Используйте отдельную тестовую базу.

Для проверки Whisper из исходников установите `requirements-whisper.txt` в отдельное окружение и запустите его `hf`:

```bash
hf download Systran/faster-whisper-small config.json model.bin tokenizer.json vocabulary.txt \
  --revision 536b0662742c02347bc0e980a01041f333bce120 --local-dir output/whisper-small
```

Направьте `TTS_CLIPS_WHISPER_PYTHON` на интерпретатор этого окружения и `TTS_CLIPS_WHISPER_MODEL` на скачанный каталог. Рецепт прежнего Windows-окружения VoxCPM2 описан в [аудите C0A](C0A_Source_Audit_and_Architecture_Proposal.md).

## Устройство приложения

- `web/` содержит React-интерфейс, `clips/main.py` обслуживает `dist/` и `/api` с одного origin. Новый бандл появляется после `npm run build`.
- `clips/store.py` хранит версии в SQLite WAL. Дубли, QC, восстановление и экспорты используют ту же базу. WAV и выпуски лежат рядом с ней в `.audio` и `.exports`. Один процесс Uvicorn владеет очередями одной базы. Для нескольких монтажёров приложение использует контроль редакций и идемпотентные команды.
- `clips/vox.py` запускает один долгоживущий воркер синтеза через JSON Lines. `clips/whisper.py` запускает CPU-воркер на проверку. FFmpeg преобразует WAV-референсы и кодирует MP3. Голоса, WAV, отчёты и выпуски имеют привязку к редакции и происхождению.
- `packaging/tts-clips.spec` описывает RPM, `packaging/setup.py` устанавливает runtime и модели, `packaging/tts-clips.service` задаёт запуск. Версия доступна в `/openapi.json`. При ошибках сначала посмотрите журнал Uvicorn и состояние задания через API.

## Тесты и RPM

```bash
.venv/bin/python -m pytest -q tests
npm run test:web
npm run build
```

Для сборки RPM на RHEL или AlmaLinux 9 x86_64 установите `rpm-build`, `python3.12` и `systemd-rpm-macros`, затем `dnf module install nodejs:22/common`. Запустите сборку из чистого checkout:

```bash
bash packaging/build-rpm.sh
rpm -qpl output/rpm/*.rpm
(cd output/rpm && sha256sum -c *.sha256)
```

Версии RC синхронизируются в `clips/__init__.py`, `package.json`, `package-lock.json` и RPM spec. `~rc1` в RPM сортируется ниже финальной версии. В RPM входит готовый фронтенд. Веса модели и Python-пакеты устанавливаются отдельно. Для воспроизведения разрешения зависимостей используйте `uv` 0.11.0, Python 3.12 и профиль x86_64 с glibc 2.34:

```bash
uv pip compile packaging/requirements-rhel.in --python-version 3.12 \
  --python-platform x86_64-manylinux_2_34 --only-binary=:all: --generate-hashes --no-header \
  -o packaging/requirements-rhel.lock
uv pip compile packaging/requirements-cuda.in --python-version 3.12 \
  --python-platform x86_64-manylinux_2_34 --only-binary=:all: --generate-hashes --no-header \
  -o packaging/requirements-cuda.lock
```

`requirements-rhel.lock` фиксирует API и Whisper, `requirements-cuda.lock` добавляет библиотеки синтеза. Установщик проверяет зависимости `pip check`, затем ставит исходник VoxCPM по коммиту из `vox_worker.py` с `--no-deps`, сохраняя метаданные pip о коммите. В lock входят библиотеки используемого inference API. Обучение, демо Gradio, denoiser и CLI VoxCPM в серверный профиль не входят. При смене коммита проверьте импорты и синтез Design/Clone на GPU. Переустановка пакетов в рабочий runtime вручную затрудняет воспроизведение отчётов QC. Для обновления меняйте lock и выпускайте новую версию RPM.

Для RC приложите RPM, SHA-256 и результаты [приёмки на сервере](Server.md#приёмка-на-сервере). Зафиксируйте окружение, отдельно укажите проверки на EL9 и проверки, выполненные на другой ОС.
