from typing import Dict, List, Tuple, Optional

class DacConfiguration:
    """
    Управление 10-битными ЦАП и тестовым мультиплексором TEST_MUX.
    """
    
    # Таблица регистров с default значениями (из документации)
    DEFAULT_REGISTERS = {
        0x8013: 0x00,
        0x8012: 0x20,
        0x8011: 0x06,
        0x8010: 0x6A,
        0x800f: 0x00,
        0x800e: 0x80,
        0x800d: 0x20,
        0x800c: 0x0A,
        0x800b: 0x01,
        0x800a: 0x80,
        0x8009: 0x80,
        0x8008: 0x20,
        0x8007: 0x02,
        0x8006: 0x01,
        0x8005: 0x80,
        0x8004: 0x60,
        0x8003: 0x28,
        0x8002: 0x08,
        0x8001: 0x02,
        0x8000: 0x00,
        # Регистры TEST_MUX
        0x801a: 0x00,   # TST_EN[4:0] = 0, TEST_MUX[31:29] = 0
        0x8019: 0x00,   # TEST_MUX[28:21]
        0x8018: 0x00,   # TEST_MUX[20:13]
        0x8017: 0x00,   # TEST_MUX[12:5]
        0x8016: 0x0F,   # TEST_MUX[4:0]=0b01111? Но 0x0F=15, биты 3-0 единицы, бит4=0, BGR_...=0
    }

    DAC_TEST_MUX_SIGNAL = {
        "DAC_CSYS_REF2": 31,
        "DAC_BUF50_CALIB": None,
        "DAC_BUF50_MUX": None,
        "DAC_BUF_LB": 28,
        "DAC_BUF_HB": 27,
        "DAC_PBUF_VB6": 26,
        "DAC_CSA_VC2": 23,
        "DAC_CSA_VC1": 22,
        "DAC_CSA_VB1": 21,
        "DAC_CSA_RES_FB": 19,
        "DAC_CMP_L": 16,
        "DAC_CMP_M": 17,
        "DAC_CMP_H": 18,
        "DAC_CMP_VC5": 25,
        "DAC_CMP_BIAS_P": 24,
        "DAC_CMP_BIAS_LSB": 20,
    }
    
    # Описание полей ЦАП (от старших битов к младшим) - как ранее
    DAC_FIELDS = {
        "DAC_CSYS_REF2": [
            (0x8013, 0, 8),   # [9:2]
            (0x8012, 6, 2),   # [1:0] в старших битах
        ],
        "DAC_BUF50_CALIB": [
            (0x8012, 0, 6),   # [9:4] младшие 6 бит
            (0x8011, 4, 4),   # [3:0] старшие 4 бита
        ],
        "DAC_BUF50_MUX": [
            (0x8011, 0, 4),   # [9:6] младшие 4 бита
            (0x8010, 2, 6),   # [5:0] биты 7-2
        ],
        "DAC_BUF_LB": [
            (0x8010, 0, 2),   # [9:8] младшие 2 бита
            (0x800f, 0, 8),   # [7:0]
        ],
        "DAC_BUF_HB": [
            (0x800e, 0, 8),   # [9:2]
            (0x800d, 6, 2),   # [1:0] старшие 2 бита
        ],
        "DAC_PBUF_VB6": [
            (0x800d, 0, 6),   # [9:4] младшие 6 бит
            (0x800c, 4, 4),   # [3:0] старшие 4 бита
        ],
        "DAC_CSA_VC2": [
            (0x800c, 0, 4),   # [9:6] младшие 4 бита
            (0x800b, 2, 6),   # [5:0] биты 7-2
        ],
        "DAC_CSA_VC1": [
            (0x800b, 0, 2),   # [9:8] младшие 2 бита
            (0x800a, 0, 8),   # [7:0]
        ],
        "DAC_CSA_VB1": [
            (0x8009, 0, 8),   # [9:2]
            (0x8008, 6, 2),   # [1:0] старшие 2 бита
        ],
        "DAC_CSA_RES_FB": [
            (0x8008, 0, 6),   # [9:4] младшие 6 бит
            (0x8007, 4, 4),   # [3:0] старшие 4 бита
        ],
        "DAC_CMP_L": [
            (0x8007, 0, 4),   # [9:6] младшие 4 бита
            (0x8006, 2, 6),   # [5:0] биты 7-2
        ],
        "DAC_CMP_M": [
            (0x8006, 0, 2),   # [9:8] младшие 2 бита
            (0x8005, 0, 8),   # [7:0]
        ],
        "DAC_CMP_H": [
            (0x8004, 0, 8),   # [9:2]
            (0x8003, 6, 2),   # [1:0] старшие 2 бита
        ],
        "DAC_CMP_VC5": [
            (0x8003, 0, 6),   # [9:4] младшие 6 бит
            (0x8002, 4, 4),   # [3:0] старшие 4 бита
        ],
        "DAC_CMP_BIAS_P": [
            (0x8002, 0, 4),   # [9:6] младшие 4 бита
            (0x8001, 2, 6),   # [5:0] биты 7-2
        ],
        "DAC_CMP_BIAS_LSB": [
            (0x8001, 0, 2),   # [9:8] младшие 2 бита
            (0x8000, 0, 8),   # [7:0]
        ],
    }
    
    # Карта TEST_MUX: для каждого бита (0..31) – (addr, shift_in_register)
    # Основано на документации:
    # 0x801a: биты [7:5] = TEST_MUX[31:29], [4:0] = TST_EN[4:0]
    # 0x8019: TEST_MUX[28:21] (8 бит) – весь регистр
    # 0x8018: TEST_MUX[20:13] (8 бит)
    # 0x8017: TEST_MUX[12:5]  (8 бит)
    # 0x8016: биты [4:0] = TEST_MUX[4:0], [5]=BGR_V_MUX, [6]=BGR_I_MUX, [7]=BGR_V_TR[7]
    TEST_MUX_MAP = {
        0: (0x8016, 3),  1: (0x8016, 4),  2: (0x8016, 5),  3: (0x8016, 6),  4: (0x8016, 7),
        5: (0x8017, 0),  6: (0x8017, 1),  7: (0x8017, 2),  8: (0x8017, 3),  9: (0x8017, 4),
        10: (0x8017, 5), 11: (0x8017, 6), 12: (0x8017, 7),
        13: (0x8018, 0), 14: (0x8018, 1), 15: (0x8018, 2), 16: (0x8018, 3), 17: (0x8018, 4),
        18: (0x8018, 5), 19: (0x8018, 6), 20: (0x8018, 7),
        21: (0x8019, 0), 22: (0x8019, 1), 23: (0x8019, 2), 24: (0x8019, 3), 25: (0x8019, 4),
        26: (0x8019, 5), 27: (0x8019, 6), 28: (0x8019, 7),
        29: (0x801a, 0), 30: (0x801a, 1), 31: (0x801a, 2)
    }
    
    def __init__(self):
        self.registers = self.DEFAULT_REGISTERS.copy()
        self._dirty = set()
    
    # -------- DAC methods (как ранее) --------
    def set_dac(self, name: str, value: int, switch_mux: bool = False) -> bool:
        """
        Установить значение ЦАП.
        Если switch_mux=True, то после установки автоматически переключает TEST_MUX
        на выход этого ЦАП (если у него есть соответствующий сигнал).
        """
        # Устанавливаем значение DAC
        if not (0 <= value <= 1023):
            raise ValueError(f"DAC value must be 0-1023, got {value}")
        if name not in self.DAC_FIELDS:
            raise KeyError(f"Unknown DAC: {name}")
        fields = self.DAC_FIELDS[name]
        remaining = value
        for addr, shift, width in reversed(fields):
            part = remaining & ((1 << width) - 1)
            current = self.registers[addr]
            mask = ((1 << width) - 1) << shift
            new_val = (current & ~mask) | ((part << shift) & mask)
            if new_val != current:
                self.registers[addr] = new_val
                self._dirty.add(addr)
            remaining >>= width
        
        # Опционально переключаем мультиплексор на выход этого DAC
        if switch_mux:
            signal = self.DAC_TEST_MUX_SIGNAL.get(name)
            if signal is not None:
                self.set_test_mux(signal)
        return True
    
    def set_dac_and_mux(self, name: str, value: int) -> bool:
        """Устанавливает DAC и переключает TEST_MUX на его выход (если есть)."""
        return self.set_dac(name, value, switch_mux=True)
    
    def get_dac(self, name: str) -> Optional[int]:
        if name not in self.DAC_FIELDS:
            return None
        fields = self.DAC_FIELDS[name]
        value = 0
        for addr, shift, width in fields:
            reg_val = self.registers[addr]
            part = (reg_val >> shift) & ((1 << width) - 1)
            value = (value << width) | part
        return value
    
    # -------- TEST_MUX methods --------
    def set_test_mux(self, signal_index: int) -> bool:
        """
        Устанавливает one-hot мультиплексор на указанный сигнал (0..31).
        Сбрасывает предыдущий выбор (все остальные биты TEST_MUX становятся 0).
        Сохраняет значения других полей (TST_EN, BGR_*).
        Возвращает True при успехе.
        """
        if not (0 <= signal_index <= 31):
            raise ValueError(f"Signal index must be 0..31, got {signal_index}")
        
        # Сначала обнулить все биты TEST_MUX во всех регистрах
        # Для этого пройдём по всем адресам, где есть биты TEST_MUX, и замаскируем их
        test_mux_addrs = {0x8016, 0x8017, 0x8018, 0x8019, 0x801a}
        # Маски для каждого регистра (какие биты принадлежат TEST_MUX)
        masks = {
            0x8016: 0b11110000,   # биты [4:0]
            0x8017: 0b11111111,   # все 8 бит
            0x8018: 0b11111111,
            0x8019: 0b11111111,
            0x801a: 0b00000111,   # биты [7:5]
        }
        for addr in test_mux_addrs:
            current = self.registers[addr]
            mask = masks[addr]
            # Обнулить только биты TEST_MUX, оставив другие поля (TST_EN, BGR_*)
            new_val = current & ~mask
            if new_val != current:
                self.registers[addr] = new_val
                self._dirty.add(addr)
        
        # Установить нужный бит
        if signal_index in self.TEST_MUX_MAP:
            addr, shift = self.TEST_MUX_MAP[signal_index]
            current = self.registers[addr]
            new_val = current | (1 << shift)
            if new_val != current:
                self.registers[addr] = new_val
                self._dirty.add(addr)
            return True
        else:
            raise KeyError(f"Signal index {signal_index} not found in TEST_MUX map")
    
    def get_test_mux(self) -> int:
        """Возвращает текущий выбранный сигнал (индекс 0..31) или -1, если не one-hot."""
        # Собрать все биты TEST_MUX в одно 32-битное число
        test_mux_val = 0
        for bit, (addr, shift) in self.TEST_MUX_MAP.items():
            reg_val = self.registers[addr]
            if (reg_val >> shift) & 1:
                test_mux_val |= (1 << bit)
        # Проверить, что one-hot (ровно один бит)
        if test_mux_val & (test_mux_val - 1) != 0:
            return -1  # больше одного бита или ноль
        if test_mux_val == 0:
            return -1
        return test_mux_val.bit_length() - 1
    
    # -------- Общие методы --------
    def get_modified_registers(self) -> Dict[int, int]:
        return {addr: self.registers[addr] for addr in self._dirty}
    
    def clear_dirty(self):
        self._dirty.clear()
    
    def all_registers(self) -> Dict[int, int]:
        return self.registers.copy()
    
    def write_to_client(self, client):
        """Отправить изменённые регистры через клиент MGPDLab."""
        for addr, value in self.get_modified_registers().items():
            if not client.write_byte(value, addr):
                print(f"Failed to write 0x{addr:04X} = 0x{value:02X}")
                return False
        self.clear_dirty()
        return True
    
    def __repr__(self):
        return f"<DacConfiguration dirty={len(self._dirty)}>"