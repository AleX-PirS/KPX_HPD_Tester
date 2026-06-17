import numpy as np
import matplotlib.pyplot as plt
import os

def load_file(code, directory='./csv_data_CMP'):
    fname = os.path.join(directory, f"{code}.csv")
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

def plot_offset_curve(directory='./csv_data_CMP', edge='both', min_amplitude=0.2):
    codes = []
    offsets = []
    for code in range(256):
        try:
            t, out, thd, inp = load_file(code, directory)
            off = compute_offset(t, out, thd, inp, edge=edge, min_amplitude=min_amplitude)
            if not np.isnan(off):
                codes.append(code)
                offsets.append(off)
        except Exception:
            continue
    if codes:
        plt.figure(figsize=(10, 5))
        plt.plot(codes, offsets, 'o-', markersize=3, linewidth=1)
        plt.xlabel('Код подстройки')
        plt.ylabel('Напряжение смещения (В)')
        plt.title(f'Калибровочная характеристика (edge={edge})')
        plt.grid(True)
        plt.show()
        return codes, offsets
    else:
        print("Не найдено ни одного кода с переключениями")
        return [], []

def plot_offset_sorted(directory='./csv_data_CMP', edge='both', min_amplitude=0.2, label_every=1):
    pairs = []
    for code in range(256):
        try:
            t, out, thd, inp = load_file(code, directory)
            off = compute_offset(t, out, thd, inp, edge=edge, min_amplitude=min_amplitude)
            if not np.isnan(off):
                pairs.append((code, off))
        except Exception:
            continue
    if not pairs:
        print("Нет данных для построения")
        return
    pairs.sort(key=lambda x: x[1])
    sorted_codes = [p[0] for p in pairs]
    sorted_offsets = [p[1] for p in pairs]
    print("Отсортированные по смещению коды (смещение в В):")
    for code, off in pairs:
        print(f"Код {code}: {off:.6f}")
    plt.figure(figsize=(12, 6))
    plt.plot(range(1, len(sorted_offsets)+1), sorted_offsets, 'o-', markersize=3, linewidth=1)
    # Добавляем подписи к точкам (код)
    for i, (code, off) in enumerate(pairs, start=1):
        if label_every > 0 and (i-1) % label_every == 0:
            plt.text(i, off, f' {code}', fontsize=8, ha='center', va='top')
    plt.xlabel('Порядковый номер (от min смещения к max)')
    plt.ylabel('Напряжение смещения (В)')
    plt.title(f'Отсортированная калибровочная характеристика (edge={edge})')
    plt.grid(True)
    plt.tight_layout()
    plt.show()
    return pairs

def plot_offset_histogram(code, directory='./csv_data_CMP', edge='both', min_amplitude=0.2, bins=20):
    t, out, thd, inp = load_file(code, directory)
    rise, fall = find_switches(out, min_amplitude)
    switches = []
    if edge in ('rise', 'both'):
        switches.extend(rise)
    if edge in ('fall', 'both'):
        switches.extend(fall)
    if len(switches) == 0:
        print(f"Код {code}: переключений не найдено")
        return
    switches = np.sort(switches)
    diffs = thd[switches] - inp[switches]
    plt.figure()
    plt.hist(diffs, bins=bins, alpha=0.7, edgecolor='black')
    plt.xlabel('THD - IN (В)')
    plt.ylabel('Частота')
    plt.title(f'Гистограмма смещений для кода {code} (edge={edge})')
    plt.grid(True, alpha=0.3)
    plt.show()

def plot_offset_histogram_all(directory='./csv_data_CMP', edge='both', min_amplitude=0.2, bins=20):
    offsets = []
    for code in range(256):
        try:
            t, out, thd, inp = load_file(code, directory)
            off = compute_offset(t, out, thd, inp, edge=edge, min_amplitude=min_amplitude)
            if not np.isnan(off):
                offsets.append(off)
        except Exception:
            continue
    if not offsets:
        print("Нет данных для построения гистограммы")
        return
    plt.figure(figsize=(8, 5))
    plt.hist(offsets, bins=bins, alpha=0.7, edgecolor='black', color='skyblue')
    plt.xlabel('Напряжение смещения (В)')
    plt.ylabel('Количество кодов')
    plt.title(f'Распределение смещений по всем кодам (edge={edge})')
    plt.grid(True, alpha=0.3)
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


DIRECTORY = 'csv_data_CMP_alt_more_stat'
EDGE = 'rise'

if __name__ == "__main__":
    # 1) Калибровочная кривая
    # plot_offset_curve(directory=DIRECTORY, edge=EDGE)
    plot_offset_sorted(directory=DIRECTORY, edge=EDGE, label_every=0)
    
    # 2) Гистограмма распределения смещений по всем кодам
    # plot_offset_histogram_all(directory=DIRECTORY, edge=EDGE, bins=30)
    
    # 3) Гистограмма для одного кода (например, 100)
    # plot_offset_histogram(directory=DIRECTORY, 135, edge=EDGE)
    
    # 4) Наложение выходов для визуального сравнения
    # plot_overlay(directory=DIRECTORY, codes='all')
    # plot_overlay(directory=DIRECTORY, codes=[0, 135, 160, 240, 255])