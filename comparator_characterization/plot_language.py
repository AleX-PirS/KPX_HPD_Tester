"""Consistent Russian/English localization for automatic matplotlib figures."""

from __future__ import annotations

from matplotlib.text import Text


_EN_TO_RU_EXACT = {
    "Raw": "Исходное",
    "Compensated": "С компенсацией",
    "Spatially compensated": "С пространственной компенсацией",
    "Measured map": "Измеренная карта",
    "Spatial plane": "Пространственная плоскость",
    "Residual after gradient removal": "Остаток после удаления градиента",
    "Local nonuniformity": "Локальная неоднородность",
    "Common component": "Общая компонента",
    "Target": "Цель эквализации",
    "Trim = 0": "Trim = 0",
    "Trim = 31": "Trim = 31",
    "Equalized": "После эквализации",
    "Baseline": "Базовая линия",
    "Mean": "Среднее",
    "Median": "Медиана",
    "Data": "Данные",
    "Fit": "Аппроксимация",
    "fit": "аппроксимация",
    "centroid": "центроид",
    "maximum": "максимум",
    "Pixel count": "Число пикселей",
    "Fit count": "Число аппроксимаций",
    "Trim code": "Код подстройки",
    "Pearson r": "Коэффициент Пирсона r",
    "Q50, electrons": "Q50, электроны",
    "Offset, V": "Смещение, В",
    "V50, V": "V50, В",
    "Sigma, mV": "Sigma, мВ",
    "Delta V50, mV": "Delta V50, мВ",
    "REF1 (fixed)": "REF1 (фиксированный)",
    "REF2 (varied)": "REF2 (изменяемый)",
    "0 normal, 1 one comparator, 2 mixed, 3 common": "0 норма, 1 один компаратор, 2 смешанный, 3 общий сдвиг",
    "Detection efficiency": "Эффективность регистрации",
    "Fit coverage": "Доля успешных аппроксимаций",
    "Injected pixels: signal counts": "Инжектируемые пиксели: сигнальные отсчеты",
    "Signal median and 10-90% repeats": "Медиана сигнала и 10-90% повторов",
    "Background q95; dotted lines are medians": "Фон q95; пунктиром показаны медианы",
    "Equalization trim-code distribution": "Распределение кодов подстройки после эквализации",
    "Noise fit-quality distribution": "Распределение качества аппроксимации шума",
    "S-curve fit-quality distribution": "Распределение качества аппроксимации S-кривых",
    "Background occupancy versus threshold": "Фоновая засветка в зависимости от порога",
    "Measured threshold distributions, logarithmic view": "Измеренные распределения порогов, логарифмический масштаб",
    "Identical physical pixel population across measured stages": "Одинаковая физическая выборка пикселей на всех этапах",
    "Accepted noise-fit widths; not an input-charge ENC measurement": "Принятые ширины шумовой кривой, не ENC во входном заряде",
    "Baseline distributions and spatial maps": "Распределения базовой линии и пространственные карты",
    "Unaligned matrix noise: mean, median and 10-90% pixel band": "Шум матрицы без выравнивания: среднее, медиана и интервал 10-90%",
    "Uniform-trim response of the owned matrix": "Отклик принадлежащей половины матрицы при едином trim-коде",
    "Threshold distributions for every uniform trim code": "Распределения порогов для каждого единого trim-кода",
    "Unaligned matrix-mean noise curves for measured trims": "Средние шумовые кривые матрицы для измеренных trim-кодов",
    "Cross-window baseline offsets and common per-pixel component": "Смещения базовой линии трех окон и общая пиксельная компонента",
    "Same-pixel baseline correlation between comparator windows": "Корреляция базовой линии одного пикселя между окнами",
    "Pearson correlation of same-pixel metrics": "Корреляция Пирсона метрик одного пикселя",
    "Baseline offset versus raw and spatially compensated gain": "Смещение базовой линии и усиление до и после пространственной компенсации",
    "Recommended comparator trims in the same physical pixels": "Рекомендуемые подстройки компараторов в одних физических пикселях",
    "Diagnostic fault candidates; classification is not a circuit diagnosis": "Диагностические кандидаты неисправностей, классификация не является схемотехническим диагнозом",
    "Shared and differential spatial profiles": "Общие и дифференциальные пространственные профили",
    "Unsupervised decomposition of AB/BC/CD baseline offsets": "Неконтролируемое разложение смещений базовой линии AB/BC/CD",
    "Gain maps across all comparator windows": "Карты усиления по всем окнам компараторов",
    "Final fixed-threshold three-window REF sweep": "Финальный REF sweep трех окон с фиксированными порогами",
    "Three windows at fixed evenly spaced physical thresholds": "Три окна с равномерно расположенными физическими порогами",
    "Threshold distributions before and after equalization": "Распределения порогов до и после эквализации",
    "Measured noise-threshold distributions; final equalization NOT measured": "Измеренные распределения шумового порога; финальная эквализация не измерена",
    "Medipix-style threshold equalization": "Эквализация порогов в стиле Medipix",
    "Measured distributions; no final equalization data": "Измеренные распределения; нет данных финальной эквализации",
    "Equalization dispersion metrics": "Метрики разброса после эквализации",
    "Endpoint dispersion only; unequal valid-pixel populations": "Только разброс крайних trim-кодов; выборки валидных пикселей различаются",
    "Median, dashed mean and 10-90% pixel band": "Медиана, пунктирное среднее и диапазон 10-90% пикселей",
    "Fit coverage": "Доля успешных аппроксимаций",
    "Pixel noise versus FCLK during blocking GET_SHOT": "Шум пикселей в зависимости от FCLK во время блокирующего GET_SHOT",
    "S-curves, median and 10-90% pixel band": "S-кривые, медиана и диапазон 10-90% пикселей",
    "Derivative of the matrix-median S-curve": "Производная медианной S-кривой матрицы",
    "REF LUT selection: one fixed REF1, varied REF2": "Выбор по REF LUT: фиксированный REF1 и изменяемый REF2",
    "Raw three-threshold consistency": "Исходная согласованность трех порогов",
    "After diagnostic plane removal": "После диагностического удаления плоскости",
    "within_population": "В пределах популяции",
    "single_comparator_offset_candidate": "Кандидат смещения одного компаратора",
    "mixed_or_differential_shift_candidate": "Кандидат смешанного или дифференциального сдвига",
    "common_CSA_or_baseline_shift_candidate": "Кандидат общего сдвига ЗЧУ или базовой линии",
    "baseline_offset_window_v": "Смещение базовой линии окна",
    "noise_sigma_v_window": "Ширина шума окна",
    "raw_gain_mv_per_ke_window": "Исходное усиление окна",
    "compensated_gain_mv_per_ke_window": "Усиление окна с компенсацией",
    "maximum_tested_linear_charge_electrons_window": "Максимальный линейный заряд окна",
    "trim_fit_window": "Код подстройки окна по fit",
    "trim_centroid_window": "Код подстройки окна по центроиду",
    "trim_maximum_window": "Код подстройки окна по максимуму",
}

_EN_TO_RU_PARTS = (
    (
        "A per-amplitude spatial plane is removed; raw gain remains primary",
        "Для каждой амплитуды удаляется пространственная плоскость; "
        "исходное усиление остается основным результатом",
    ),
    (
        "Diagnostic baseline/IR-drop compensation",
        "Диагностическая компенсация базовой линии/IR-drop",
    ),
    (
        "Paired background: retained baseline-noise points",
        "Парный фон: сохраненные точки шумовой базовой линии",
    ),
    (
        "Recommended comparator trims in the same physical pixels",
        "Рекомендуемые подстройки компараторов в одних физических пикселях",
    ),
    (
        "Charge at 50% response for three fixed thresholds",
        "Заряд при 50% отклика для трех фиксированных порогов",
    ),
    (
        "Per-pixel nominal charge response",
        "Попиксельный отклик на номинальный заряд",
    ),
    (
        "Raw S-curve counts",
        "Исходные отсчеты S-кривой",
    ),
    (
        "raw S-curve counts",
        "исходные отсчеты S-кривой",
    ),
    (
        "measured positive branch",
        "измеренная положительная ветвь",
    ),
    (
        "Injection-density coupling diagnostics",
        "Диагностика связи с плотностью инжекции",
    ),
    (
        "Injection-density shift",
        "Сдвиг из-за плотности инжекции",
    ),
    (
        "AB/BC/CD trim distributions",
        "Распределения подстроек AB/BC/CD",
    ),
    (
        "Compensated - raw gain",
        "Усиление с компенсацией минус исходное усиление",
    ),
    (
        "S-curve sigma, threshold DAC codes",
        "Sigma S-кривой, коды порогового ЦАП",
    ),
    (
        "Inactive excess hit fraction",
        "Доля избыточных отсчетов неактивных пикселей",
    ),
    (
        "Active pixels per shot",
        "Активных пикселей на снимок",
    ),
    (
        "Sigma / reference sigma - 1",
        "Sigma / опорная sigma - 1",
    ),
    (
        "Median |delta V50|",
        "Медиана |delta V50|",
    ),
    (
        "Median sigma / reference sigma",
        "Медианная sigma / опорная sigma",
    ),
    (
        "noise before/after equalization",
        "шум до и после эквализации",
    ),
    (
        "measured noise curves",
        "измеренные шумовые кривые",
    ),
    ("REF1 span", "Диапазон REF1"),
    ("amplitude response", "амплитудный отклик"),
    ("for three fixed thresholds", "для трех фиксированных порогов"),
    ("Pearson ", "Пирсон "),
    ("Physical ASIC column", "Физический столбец ASIC"),
    ("Physical ASIC row", "Физическая строка ASIC"),
    ("Physical column", "Физический столбец"),
    ("Physical row", "Физическая строка"),
    ("Threshold DAC code", "Код порогового ЦАП"),
    ("Pixel C", "Пиксель C"),
    ("Threshold voltage", "Напряжение порога"),
    ("Effective threshold voltage", "Эффективное напряжение порога"),
    ("Effective threshold", "Эффективный порог"),
    ("Threshold dispersion", "Разброс порога"),
    ("Raw decoded count", "Исходное декодированное число отсчетов"),
    ("Decoded counter value", "Декодированное значение счетчика"),
    ("Background-subtracted count", "Отсчеты после вычитания фона"),
    ("Background count", "Фоновые отсчеты"),
    ("Pixel count", "Число пикселей"),
    ("Pixel count, log scale", "Число пикселей, логарифмический масштаб"),
    ("Selected local trim code", "Выбранный локальный trim-код"),
    ("Trim code", "Код подстройки"),
    ("Uniform trim code", "Единый trim-код"),
    ("Fitted noise width", "Аппроксимированная ширина шума"),
    ("Noise width", "Ширина шума"),
    ("Detection efficiency", "Эффективность регистрации"),
    ("Median efficiency", "Медианная эффективность"),
    ("Nominal injected charge", "Номинальный инжектированный заряд"),
    ("Maximum tested linear charge", "Максимальный проверенный линейный заряд"),
    ("Injected amplitude", "Инжектированная амплитуда"),
    ("Nominal gain", "Номинальное усиление"),
    ("Raw gain", "Исходное усиление"),
    ("Spatially compensated gain", "Усиление с пространственной компенсацией"),
    ("Baseline offset", "Смещение базовой линии"),
    ("Median baseline offset", "Медианное смещение базовой линии"),
    ("Effective baseline", "Эффективная базовая линия"),
    ("Detrended residual", "Остаток после удаления градиента"),
    ("Measurement FCLK", "FCLK измерения"),
    ("Successful-fit fraction", "Доля успешных аппроксимаций"),
    ("Usable pixel-fit fraction", "Доля пригодных пиксельных аппроксимаций"),
    ("Amplitude index", "Индекс амплитуды"),
    ("Selection error", "Ошибка выбора"),
    ("Requested REF1-REF2 step", "Запрошенная ступенька REF1-REF2"),
    ("Selected measured step", "Выбранная измеренная ступенька"),
    ("Measured LUT voltage", "Измеренное напряжение LUT"),
    ("PCA component", "Компонента PCA"),
    ("Explained variance", "Объясненная дисперсия"),
    ("Pearson correlation", "Корреляция Пирсона"),
    ("Spearman", "Спирмен"),
    ("offset, mV", "смещение, мВ"),
    ("Offset from window median", "Отклонение от медианы окна"),
    ("pattern", "режим"),
    ("S-curves", "S-кривые"),
    ("S-curve width", "Ширина S-кривой"),
    ("S-curve parameters", "Параметры S-кривой"),
    ("S-curve fit", "Аппроксимация S-кривой"),
    ("S-curve data", "Данные S-кривой"),
    ("Noise curves", "Шумовые кривые"),
    ("noise curves", "шумовые кривые"),
    ("trim transfer", "зависимость от кода подстройки"),
    ("all trims", "всех кодов подстройки"),
    ("repeat mean and SEM", "среднее по повторам и SEM"),
    ("Matrix amplitude response", "Амплитудный отклик матрицы"),
    ("Owned-matrix response", "Отклик принадлежащей половины матрицы"),
    ("Spatial effective-baseline analysis", "Пространственный анализ эффективной базовой линии"),
    ("row min->max", "строка min->max"),
    ("column min->max", "столбец min->max"),
    ("Matrix S-curves versus measurement FCLK", "S-кривые матрицы при разных FCLK измерения"),
    ("FCLK comparison", "сравнение FCLK"),
    ("Measurement FCLK", "FCLK измерения"),
    ("Recommended comparator trims", "Рекомендуемые подстройки компараторов"),
    ("trim distributions", "распределения подстройки"),
    ("fit trim", "подстройка по fit"),
    ("centroid trim", "подстройка по центроиду"),
    ("maximum trim", "подстройка по максимуму"),
    ("Raw baseline intercept", "Исходное пересечение базовой линии"),
    ("Compensated baseline intercept", "Пересечение базовой линии с компенсацией"),
    ("Raw three-threshold R2", "Исходный R2 по трем порогам"),
    ("Compensated three-threshold R2", "R2 по трем порогам с компенсацией"),
    ("Charge at 50% response", "Заряд при 50% отклика"),
    ("raw and diagnostic plane removal", "исходные данные и диагностическое удаление плоскости"),
    ("Largest tested charge consistent with the three-threshold line model", "Максимальный проверенный заряд, согласующийся с линейной моделью трех порогов"),
    ("Pixel median", "Медиана пикселей"),
    ("Signal median", "Медиана сигнала"),
    ("Signal 10-90% pixels", "Диапазон 10-90% сигнала по пикселям"),
    ("Signal q95", "q95 сигнала"),
    ("Background median", "Медиана фона"),
    ("Background q90", "q90 фона"),
    ("Background q95", "q95 фона"),
    ("Background maximum", "Максимум фона"),
    ("Matrix-mean counter value", "Среднее значение счетчика матрицы"),
    ("Noise-curve fit", "Аппроксимация шумовой кривой"),
    ("Gaussian fit", "Гауссова аппроксимация"),
    ("Effective analysis N", "Эффективное N анализа"),
    ("10-90% pixels", "10-90% пикселей"),
    ("row/column median", "медиана строки/столбца"),
    ("plane model", "модель плоскости"),
    ("valid fits", "валидных аппроксимаций"),
    ("mean", "среднее"),
    ("median", "медиана"),
    ("data", "данные"),
    ("fit", "аппроксимация"),
    ("trim", "подстройка"),
    ("pattern", "режим"),
    ("step", "ступенька"),
    ("electrons", "электроны"),
)

_EN_TO_RU_PARTS = (
    ("Threshold voltage, mV", "Пороговое напряжение, мВ"),
    ("Signal - background, counts", "Сигнал - фон, отсчеты"),
    ("Inactive noise change", "Изменение шума неинжектируемых пикселей"),
    ("measured noise curves", "Измеренные шумовые кривые"),
    ("Trim transfer", "Характеристика подстройки"),
    ("Signal counts", "Сигнальный счет"), ("No data", "Нет данных"),
    ("V50, mV", "V50, мВ"), ("Pixel C", "Пиксель C"),
    ("amplitude response", "амплитудный отклик"),
    (" MHz", " МГц"), (" mV", " мВ"), (" ke", " тыс. электронов"),
) + _EN_TO_RU_PARTS

_RU_TO_EN_EXACT = {value: key for key, value in _EN_TO_RU_EXACT.items()}
_RU_TO_EN_PARTS = tuple((right, left) for left, right in _EN_TO_RU_PARTS)


def translate_plot_text(value: str, language: str) -> str:
    """Translate known plot vocabulary while preserving numbers and units."""

    if language == "ru":
        if value in _EN_TO_RU_EXACT:
            return _EN_TO_RU_EXACT[value]
        replacements = _EN_TO_RU_PARTS
    elif language == "en":
        if value in _RU_TO_EN_EXACT:
            return _RU_TO_EN_EXACT[value]
        replacements = _RU_TO_EN_PARTS
    else:
        raise ValueError("plot language must be ru or en")
    result = value
    for source, target in replacements:
        result = result.replace(source, target)
    return result


def localize_figure(figure, language: str, square_physical_pixels=None) -> None:
    """Localize every visible matplotlib text object immediately before save."""

    if square_physical_pixels is not None:
        prepare_matrix_geometry(figure, square_physical_pixels)
    for item in figure.findobj(match=Text):
        current = item.get_text()
        if current:
            item.set_text(translate_plot_text(current, language))


class _AlignedColorbar:
    """Picklable locator, evaluated after the image axes apply their aspect."""
    def __init__(self, parents):
        self.parents = parents

    def __call__(self, axis, renderer):
        from matplotlib.transforms import Bbox
        for parent in self.parents:
            parent.apply_aspect()
        box = Bbox.union([parent.get_position() for parent in self.parents])
        return Bbox.from_bounds(box.x1 + .025 * box.width, box.y0,
                                .045 * box.width / max(1, len(self.parents)), box.height)


def prepare_matrix_geometry(figure, square_physical_pixels=False):
    for axis in figure.axes:
        for image in axis.images:
            shape = image.get_array().shape
            if len(shape)<2 or shape[:2] != (32,16):
                continue
            axis.set_aspect("auto")
            axis.set_box_aspect(2 if square_physical_pixels else 1)
            colorbar = image.colorbar
            if colorbar is not None:
                parents = getattr(colorbar.ax, "_colorbar_info", {}).get("parents", [axis])
                colorbar.ax.set_box_aspect(None)
                colorbar.ax.set_aspect("auto")
                colorbar.ax.set_axes_locator(_AlignedColorbar(parents))
