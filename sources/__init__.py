# Реестр источников: код источника -> (модуль, доп. параметры, страна закупки)
ADAPTERS = {
    "pcardin_tr": ("sources.akinon", {"site": "pcardin_tr"}, "TR"),
    "cacharel_tr": ("sources.akinon", {"site": "cacharel_tr"}, "TR"),
    "trendyol": ("sources.trendyol", {}, "TR"),
    "yoox": ("sources.yoox", {}, "IT"),
    "yoox_import": ("sources.yoox_import", {}, "IT"),   # файлы от кнопки «Сохранить YOOX»
    # партнёрский фид (Awin и др.) — вместо yoox_import, когда будет доступ; не включать оба сразу
    "feed_yoox": ("sources.feed_yoox", {"product_source": "yoox"}, "IT"),
}
