"""
Шаг 0 из MAP_MODEL_SPEC.md — префлайт-проверки данных для будущей модели MAP.

Читает матрицу через common.load_matrix() и прогоняет по ней набор
ассертов (структура колонок, объёмы, допустимые значения, размер чистой
зоны, согласованность survived/outcome). Ничего не чинит и не подгоняет —
если ассерт падает, скрипт должен упасть с трейсбеком.

Запуск (из корня репозитория):
    python -m models.map_preflight
"""
import pandas as pd

from models import common


def main():
    mx = common.load_matrix()

    # 0.1 структура
    required_cols = {
        "sub_id", "step_k", "outcome", "weeks_obs", "app_id", "geo",
        "media_source", "plan_interval", "billing_day_of_month",
        "survived",
    }
    missing = required_cols - set(mx.columns)
    assert not missing, f"в матрице отсутствуют обязательные колонки: {missing}"

    # 0.2 объёмы (ориентиры текущего снапшота, допуск +-20% на будущие пересборки)
    n_rows = len(mx)
    assert 2_000_000 < n_rows < 3_500_000, (
        f"неожиданное число строк матрицы: {n_rows} (ожидалось (2_000_000, 3_500_000))"
    )
    n_subs = mx["sub_id"].nunique()
    assert 250_000 < n_subs < 450_000, (
        f"неожиданное число уникальных sub_id: {n_subs} (ожидалось (250_000, 450_000))"
    )

    # 0.3 значения
    step_k_min = mx["step_k"].min()
    assert step_k_min == 1, f"минимальный step_k должен быть 1, получено {step_k_min}"

    allowed_outcomes = {"renewed", "recovered", "voluntary_cancel", "billing_issue", "churned"}
    actual_outcomes = set(mx["outcome"].unique())
    assert actual_outcomes <= allowed_outcomes, (
        f"неожиданные значения outcome: {actual_outcomes - allowed_outcomes}"
    )

    assert (mx["weeks_obs"] >= 0).all(), (
        f"найдены отрицательные weeks_obs: {(mx['weeks_obs'] < 0).sum()} строк"
    )

    bad_billing_day = ~mx["billing_day_of_month"].between(1, 31)
    assert not bad_billing_day.any(), (
        f"billing_day_of_month вне диапазона 1-31: {bad_billing_day.sum()} строк"
    )

    # 0.4 чистая зона достаточна
    clean_zone = mx[(mx["step_k"] <= 8) & (mx["weeks_obs"] >= mx["step_k"])]
    assert len(clean_zone) > 1_000_000, (
        f"чистая зона подозрительно мала: {len(clean_zone)} строк (ожидалось > 1_000_000)"
    )

    # 0.5 survived согласован с outcome
    expected_survived = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mismatch = mx["survived"] != expected_survived
    assert not mismatch.any(), (
        f"survived не согласован с outcome в {mismatch.sum()} строках"
    )

    print("Все ассерты Шага 0 пройдены успешно.")
    print()

    # --- Дополнительная распечатка для владельца (не ассерты) ---

    none_geo_share = (mx["geo"] == "(none)").mean() * 100
    print(f"Доля geo == '(none)': {none_geo_share:.2f}%")

    unknown_media_share = (mx["media_source"] == "unknown").mean() * 100
    print(f"Доля media_source == 'unknown': {unknown_media_share:.2f}%")

    print()
    print("Распределение plan_interval:")
    print(mx["plan_interval"].value_counts())

    print()
    print("Число строк чистой зоны (step_k <= 8, weeks_obs >= step_k) по ступеням 1-8:")
    for k in range(1, 9):
        n = len(clean_zone[clean_zone["step_k"] == k])
        print(f"  step_k={k}: {n}")

    # --- Задача А: диагностика пересечения geo=="(none)" x media_source=="unknown" ---
    # Владелец попросил зафиксировать цифрой наблюдение: доля geo=="(none)" и доля
    # media_source=="unknown" совпадают (~14.12% каждая) — похоже, это одна и та же
    # подгруппа безатрибуционных юзеров. Механику residual-цепочки не меняем — это
    # чисто диагностическая печать.
    print()
    print("=== Диагностика: пересечение geo=='(none)' и media_source=='unknown' ===")

    is_none_geo = mx["geo"] == "(none)"
    is_unknown_media = mx["media_source"] == "unknown"
    both = is_none_geo & is_unknown_media
    n_both = both.sum()
    share_both = both.mean() * 100
    print(f"Строк с geo=='(none)' И media_source=='unknown' одновременно: {n_both} ({share_both:.2f}% от общего числа строк)")

    print()
    print("Кросс-таблица 2x2: geo (none / не-none) x media_source (unknown / не-unknown):")
    geo_label = is_none_geo.map({True: "geo=(none)", False: "geo!=(none)"})
    media_label = is_unknown_media.map({True: "media_source=unknown", False: "media_source!=unknown"})
    cross = pd.crosstab(geo_label, media_label)
    print(cross)


if __name__ == "__main__":
    main()
