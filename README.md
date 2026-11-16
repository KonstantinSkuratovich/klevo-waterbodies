# Открытая база водоёмов Беларуси

Справочник водоёмов и границ областей для приложения «Клёво, где?» и скрипт, которым он собран. Всё здесь публикуется
как есть, под лицензией ODbL 1.0.

**Данные © участники OpenStreetMap** (OpenStreetMap contributors), лицензия
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/); о правилах использования данных —
<https://www.openstreetmap.org/copyright>. Файлы `data/waterbodies.json` и `data/oblasts.json` — производная база от
выгрузки OpenStreetMap и распространяются на тех же условиях (Share alike, раздел 4.4 лицензии): при использовании
сохраните эту подпись и лицензию и опубликуйте свои изменения базы под ODbL.

## Что в папке

| Файл | Что это |
|------|---------|
| `build_waterbodies.py` | Скрипт сборки: из выгрузки OpenStreetMap делает обе базы. Только стандартная библиотека Python 3.10+. |
| `osm_reader.py` | Чтение выгрузок `.osm` и `.osm.pbf` без внешних библиотек. |
| `data/waterbodies.json` | Справочник: код записи (`way/N`, `relation/N`), название, тип, центр, область, площадь. |
| `data/oblasts.json` | Упрощённые границы областей (допуск 10 м): по ним определяется область точки. |
| `LICENSE-ODbL` | Полный текст лицензии ODbL 1.0. |

## Как пересобрать

Из корня этой папки (`build_waterbodies.py` лежит рядом):

```bash
python build_waterbodies.py belarus-latest.osm.pbf \
    -o data/waterbodies.json --source "Geofabrik belarus-latest, 2026-11-10"
```

Выгрузка Беларуси берётся, например, у Geofabrik (<https://download.geofabrik.de/europe/belarus.html>). Результат не
зависит от порядка элементов в выгрузке: повторный запуск даёт тот же файл.

Отбор: озёра, водохранилища, пруды и старицы с названием и реки длиннее 20 км с названием; сточные водоёмы, бассейны,
шлюзы и каналы не входят.
