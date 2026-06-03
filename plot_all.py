import matplotlib.pyplot as plt
import csv
from pathlib import Path

def plot_all_dac_scans(folder_path='./csv_data/', output_file=None,
                       title="DAC Transfer Characteristics",
                       xlabel="DAC Code",
                       ylabel="Measured Value (V)",
                       figsize=(12, 8)):
    """
    Собирает все CSV файлы из папки, каждый CSV должен содержать колонки:
    DAC_code, Measured_value  (с заголовком). Данные уже отсортированы по коду.
    Легенда берётся из имени файла (без расширения).
    """
    folder = Path(folder_path)
    csv_files = list(folder.glob("*.csv"))
    if not csv_files:
        print(f"Нет CSV файлов в папке {folder_path}")
        return

    plt.figure(figsize=figsize)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.grid(True, alpha=0.3)

    for file in csv_files:
        try:
            # Читаем CSV
            with open(file, 'r') as f:
                reader = csv.reader(f)
                header = next(reader)  # пропускаем заголовок
                # Проверяем, что заголовок соответствует ожидаемому (необязательно)
                # Собираем данные
                codes = []
                values = []
                for row in reader:
                    if len(row) >= 2:
                        try:
                            code = int(row[0])
                            value = float(row[1])
                            codes.append(code)
                            values.append(value)
                        except ValueError:
                            pass  # игнорируем некорректные строки
            if not codes:
                print(f"Пропуск {file.name}: нет данных")
                continue

            label = file.stem  # имя файла без расширения
            plt.plot(codes, values, marker='.', linestyle='-', linewidth=1, markersize=2, label=label)

        except Exception as e:
            print(f"Ошибка при чтении {file.name}: {e}")

    plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left', fontsize='small')
    plt.tight_layout()

    if output_file:
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"График сохранён в {output_file}")
    plt.show()

plot_all_dac_scans()