import numpy as np
import matplotlib.pyplot as plt
import os
from typing import Union, List
import csv
import concurrent.futures

def load_file(code, directory: str):
    fname = os.path.join(directory, f"{code}.csv")
    try:
        data = np.loadtxt(fname, delimiter=',', skiprows=1)
        return data[:, 0], data[:, 1], data[:, 2], data[:, 3]
    except ValueError:
        data = np.loadtxt(fname, delimiter=',')
        return data[:, 0], data[:, 1], data[:, 2], data[:, 3]

def find_switches(out, min_amplitude=0.2):
    low = np.median(out[out < np.median(out)])
    high = np.median(out[out > np.median(out)])
    if high - low < min_amplitude:
        return [], []
    threshold = (low + high) / 2
    binary = (out > threshold).astype(int)
    diff = np.diff(binary)
    rise = np.where(diff == 1)[0]
    fall = np.where(diff == -1)[0]
    if len(rise) + len(fall) < 2:
        return [], []
    return rise, fall

def compute_offset(time, out, thd, inp, edge='both', min_amplitude=0.2):
    rise, fall = find_switches(out, min_amplitude)
    switches = []
    if edge in ('rise', 'both'):
        switches.extend(rise)
    if edge in ('fall', 'both'):
        switches.extend(fall)
    if len(switches) == 0:
        return np.nan
    switches = np.sort(switches)
    diffs = thd[switches] - inp[switches]
    return np.mean(diffs)

def process_code(args):
    """Вспомогательная функция для параллельной обработки одного кода."""
    folder, code, edge, min_amplitude = args
    try:
        t, out, thd, inp = load_file(code, folder)
        off = compute_offset(t, out, thd, inp, edge=edge, min_amplitude=min_amplitude)
        if not np.isnan(off):
            return (code, off)
    except Exception:
        pass
    return None

def collect_data_from_folders(directories, edge, min_amplitude, max_workers=None):
    """
    Параллельно обрабатывает все коды для каждой папки.
    Возвращает словарь {folder: list of (code, offset)}.
    """
    if isinstance(directories, str):
        directories = [directories]
    
    results = {}
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_folder = {}
        for folder in directories:
            for code in range(256):
                args = (folder, code, edge, min_amplitude)
                future = executor.submit(process_code, args)
                future_to_folder[future] = folder
        
        # Собираем результаты
        for future in concurrent.futures.as_completed(future_to_folder):
            folder = future_to_folder[future]
            result = future.result()
            if result is not None:
                if folder not in results:
                    results[folder] = []
                results[folder].append(result)
    return results

def plot_offset_curve(directories: Union[str, List[str]] = './csv_data_CMP', edge='both', min_amplitude=0.2):
    """
    Строит калибровочные кривые для каждой папки на одном графике.
    Использует параллельную обработку.
    """
    data = collect_data_from_folders(directories, edge, min_amplitude)
    if not data:
        print("Нет данных для построения")
        return
    
    plt.figure(figsize=(10, 5))
    for folder, pairs in data.items():
        pairs.sort(key=lambda x: x[0])
        codes = [p[0] for p in pairs]
        offsets = [p[1] for p in pairs]
        plt.plot(codes, offsets, 'o-', markersize=3, linewidth=1, label=os.path.basename(folder))
    
    plt.xlabel('Код подстройки')
    plt.ylabel('Напряжение смещения (В)')
    plt.title(f'Калибровочная характеристика (edge={edge})')
    plt.grid(True)
    if len(data) > 1:
        plt.legend()
    plt.show()

def plot_offset_sorted(directories: Union[str, List[str]] = './csv_data_CMP', edge='both', min_amplitude=0.2, label_every=1):
    """
    Строит отсортированные калибровочные кривые для каждой папки на одном графике.
    """
    data = collect_data_from_folders(directories, edge, min_amplitude)
    if not data:
        print("Нет данных для построения")
        return
    
    plt.figure(figsize=(12, 6))
    for folder, pairs in data.items():
        pairs.sort(key=lambda x: x[1])
        sorted_offsets = [p[1] for p in pairs]
        x_vals = range(1, len(sorted_offsets)+1)
        plt.plot(x_vals, sorted_offsets, 'o-', markersize=3, linewidth=1, label=os.path.basename(folder))
        if label_every > 0:
            for i, (code, off) in enumerate(pairs, start=1):
                if (i-1) % label_every == 0:
                    plt.text(i, off, f' {code}', fontsize=8, ha='center', va='top')
    
    plt.xlabel('Порядковый номер (от min смещения к max)')
    plt.ylabel('Напряжение смещения (В)')
    plt.title(f'Отсортированная калибровочная характеристика (edge={edge})')
    plt.grid(True)
    if len(data) > 1:
        plt.legend()
    plt.tight_layout()
    plt.show()

def plot_offset_histogram(code, directories: Union[str, List[str]] = './csv_data_CMP', edge='both', min_amplitude=0.2, bins=20):
    """
    Строит гистограммы смещений для заданного кода из всех папок на одном графике.
    Здесь параллельная обработка не даст выигрыша (всего несколько папок), но можно сделать.
    """
    if isinstance(directories, str):
        directories = [directories]
    
    plt.figure()
    # Для простоты оставим последовательную обработку (или можно распараллелить по папкам, но не критично)
    for folder in directories:
        try:
            t, out, thd, inp = load_file(code, folder)
        except FileNotFoundError:
            print(f"Файл {code}.csv не найден в {folder}")
            continue
        rise, fall = find_switches(out, min_amplitude)
        switches = []
        if edge in ('rise', 'both'):
            switches.extend(rise)
        if edge in ('fall', 'both'):
            switches.extend(fall)
        if len(switches) == 0:
            print(f"Код {code} в {folder}: переключений не найдено")
            continue
        switches = np.sort(switches)
        diffs = thd[switches] - inp[switches]
        plt.hist(diffs, bins=bins, alpha=0.5, edgecolor='black', label=os.path.basename(folder))
    
    plt.xlabel('THD - IN (В)')
    plt.ylabel('Частота')
    plt.title(f'Гистограмма смещений для кода {code} (edge={edge})')
    plt.grid(True, alpha=0.3)
    if len(directories) > 1:
        plt.legend()
    plt.show()

def plot_offset_histogram_all(directories: Union[str, List[str]] = './csv_data_CMP', edge='both', min_amplitude=0.2, bins=20):
    """
    Строит распределение смещений по всем кодам для каждой папки на одном графике.
    Использует параллельную обработку.
    """
    data = collect_data_from_folders(directories, edge, min_amplitude)
    if not data:
        print("Нет данных для построения гистограммы")
        return
    
    plt.figure(figsize=(8, 5))
    for folder, pairs in data.items():
        offsets = [p[1] for p in pairs]
        plt.hist(offsets, bins=bins, alpha=0.5, edgecolor='black', label=os.path.basename(folder))
    
    plt.xlabel('Напряжение смещения (В)')
    plt.ylabel('Количество кодов')
    plt.title(f'Распределение смещений по всем кодам (edge={edge})')
    plt.grid(True, alpha=0.3)
    if len(data) > 1:
        plt.legend()
    plt.show()

def plot_overlay(codes, directory='./csv_data_CMP', t_lim=None, decimate=1, alpha=1.0):
    if codes == 'all':
        codes = []
        for code in range(256):
            try:
                t, out, _, _ = load_file(code, directory)
                if len(find_switches(out)[0]) + len(find_switches(out)[1]) > 0:
                    codes.append(code)
            except Exception:
                continue
        if not codes:
            print("Нет кодов с переключениями")
            return
        print(f"Найдено {len(codes)} кодов с переключениями (будут показаны все)")
        alpha = 0.5
    if not codes:
        return
    t_ref, _, thd_ref, inp_ref = load_file(codes[0], directory)
    if decimate > 1:
        t_ref = t_ref[::decimate]
        thd_ref = thd_ref[::decimate]
        inp_ref = inp_ref[::decimate]
    if t_lim is not None:
        mask = (t_ref >= t_lim[0]) & (t_ref <= t_lim[1])
        t_ref = t_ref[mask]
        thd_ref = thd_ref[mask]
        inp_ref = inp_ref[mask]
    plt.figure(figsize=(12, 5))
    plt.plot(t_ref, thd_ref, 'b-', label='THD', linewidth=1)
    plt.plot(t_ref, inp_ref, 'g-', label='IN', linewidth=1)
    if len(codes) <= 10:
        colors = plt.cm.tab10(np.linspace(0, 1, len(codes)))
    else:
        colors = [plt.cm.viridis(i / len(codes)) for i in range(len(codes))]
    for i, code in enumerate(codes):
        t, out, _, _ = load_file(code, directory)
        if decimate > 1:
            t = t[::decimate]
            out = out[::decimate]
        if t_lim is not None:
            mask = (t >= t_lim[0]) & (t <= t_lim[1])
            t = t[mask]
            out = out[mask]
        plt.plot(t, out, color=colors[i], linewidth=0.8, alpha=alpha, label=f'Код {code}' if len(codes) <= 10 else "")
    plt.xlabel('Время (с)')
    plt.ylabel('Напряжение (В)')
    plt.title('Сравнение моментов переключения компаратора')
    plt.grid(True, alpha=0.3)
    if len(codes) <= 10:
        plt.legend(loc='upper right')
    plt.tight_layout()
    plt.show()

    import csv

def save_offset_data_to_csv(directories, output_filename, edge='both', min_amplitude=0.2):
    """
    Сохраняет в CSV‑файл смещения для всех кодов, для которых есть данные хотя бы в одной папке.
    Первый столбец – trim_code, остальные – имена папок.
    Если для какого‑то кода в папке нет данных, ячейка остаётся пустой.
    """
    data = collect_data_from_folders(directories, edge, min_amplitude)
    if not data:
        print("Нет данных для сохранения")
        return

    # Преобразуем в словарь {folder: {code: offset}}
    folder_offsets = {}
    all_codes = set()
    for folder, pairs in data.items():
        d = {code: off for code, off in pairs}
        folder_offsets[folder] = d
        all_codes.update(d.keys())

    # Сортируем коды по возрастанию
    sorted_codes = sorted(all_codes)
    headers = ['trim_code'] + [os.path.basename(f) for f in folder_offsets.keys()]

    with open(output_filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for code in sorted_codes:
            row = [code]
            for folder in folder_offsets.keys():
                off = folder_offsets[folder].get(code)
                row.append('' if off is None else f"{off:.6f}")
            writer.writerow(row)

    print(f"Сохранено {len(sorted_codes)} кодов в {output_filename}")


# DIRECTORY = 'csv_data_CMP_VB5700_LSB100_VC5470'
DIRECTORY = [
    'csv_data_CMP_ALT_VB5_700_LSB_100_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_200_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_300_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_400_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_500_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_600_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_700_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_800_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_900_VC5_470',
    'csv_data_CMP_ALT_VB5_700_LSB_1000_VC5_470',
]
# DIRECTORY = [
#     'csv_data_CMP_VB5_700_LSB_100_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_200_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_300_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_400_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_500_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_600_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_700_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_800_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_900_VC5_470',
#     'csv_data_CMP_VB5_700_LSB_1000_VC5_470',
# ]
EDGE = 'rise'

if __name__ == "__main__":
    # 1) Калибровочная кривая
    plot_offset_curve(directories=DIRECTORY, edge=EDGE, min_amplitude=0.1)
    # plot_offset_sorted(directories=DIRECTORY, edge=EDGE, label_every=0)
    
    # 2) Гистограмма распределения смещений по всем кодам
    # plot_offset_histogram_all(directories=DIRECTORY, edge=EDGE, bins=30)
    
    # 3) Гистограмма для одного кода (например, 100)
    # plot_offset_histogram(directories=DIRECTORY, 135, edge=EDGE)
    
    # 4) Наложение выходов для визуального сравнения
    # plot_overlay(directory=DIRECTORY, codes='all')
    # plot_overlay(directory=DIRECTORY, codes=[0, 135, 160, 240, 255])
    # save_offset_data_to_csv(directories=DIRECTORY, output_filename="offset_pixel_CMP_ALT.csv", edge=EDGE, min_amplitude=0.1)