AMUX_SIGNALS = {
    "IN_CMP": 0,
    "IN_CSA": 1,
    "MUX_TST_SIG": 2,
    "DAC_TST_REF2": 3,
    "DAC_TST_REF1": 4,
    "TST_SH": 5,
    "TST_CSA": 6,
    "TST_CMP": 7,
    "TST_PIX_CMPA": 8,
    "TST_PIX_CMPB": 9,
    "TST_PIX_CMPC": 10,
    "TST_PIX_CMPD": 11,
    "TST_CMPD": 12,
    "DAC_CMP_A": 13,
    "DAC_CMP_B": 14,
    "DAC_CMP_C": 15,
    "DAC_CMP_D": 16,
    "DAC_CMP_BIAS_LSB": 17,
    "DAC_CMP_VB5": 18,
    "DAC_CMP_VC5": 19,
    "TST_OUT": 20,
    "DAC_SH_VB4": 21,
    "DAC_SH_VB3": 22,
    "DAC_CSA_VB2": 23,
    "DAC_CSA_VB1": 24,
    "DAC_CSA_RES_FB": 25,
    "DAC_CSA_VREF": 26,
    "DAC_SH_VC4": 27,
    "DAC_SH_VC3": 28,
    "DAC_CSA_VC2": 29,
    "DAC_CSA_VC1": 30,
    "TST_SIG": 31,
    "TST_IN": 32,
    "DAC_PFB": 33,
    "DAC_BUF_HB": 34,
    "DAC_BUF_LB": 35,
    "VBG_V": 36,
    "VBGC_V": 37,
    "VBG_I": 38,
    "VBGC_I": 39,
}


REGS_FIELDS = {
    "DAC_CSA_RES_FB": [
        (0x8000, 0, 8),
        (0x8001, 4, 2),
    ],
    "DAC_CSA_RES_FB_TR": [
        (0x8001, 0, 4),
    ],

    "DAC_CSA_VB1": [
        (0x8002, 0, 8),
        (0x8003, 4, 2),
    ],
    "DAC_CSA_VB1_TR": [
        (0x8003, 0, 4),
    ],

    "DAC_CSA_VC1": [
        (0x8004, 0, 8),
        (0x8005, 4, 2),
    ],
    "DAC_CSA_VC1_TR": [
        (0x8005, 0, 4),
    ],

    "DAC_CSA_VC2": [
        (0x8006, 0, 8),
        (0x8007, 4, 2),
    ],
    "DAC_CSA_VC2_TR": [
        (0x8007, 0, 4),
    ],

    "DAC_CSA_VB2": [
        (0x8008, 0, 8),
        (0x8009, 4, 2),
    ],
    "DAC_CSA_VB2_TR": [
        (0x8009, 0, 4),
    ],

    "DAC_CSA_VREF": [
        (0x800A, 0, 8),
        (0x800B, 4, 2),
    ],
    "DAC_CSA_VREF_TR": [
        (0x800B, 0, 4),
    ],

    "DAC_SH_VB3": [
        (0x800C, 0, 8),
        (0x800D, 4, 2),
    ],
    "DAC_SH_VB3_TR": [
        (0x800D, 0, 4),
    ],

    "DAC_SH_VC3": [
        (0x800E, 0, 8),
        (0x800F, 4, 2),
    ],
    "DAC_SH_VC3_TR": [
        (0x800F, 0, 4),
    ],

    "DAC_SH_VB4": [
        (0x8010, 0, 8),
        (0x8011, 4, 2),
    ],
    "DAC_SH_VB4_TR": [
        (0x8011, 0, 4),
    ],

    "DAC_SH_VC4": [
        (0x8012, 0, 8),
        (0x8013, 4, 2),
    ],
    "DAC_SH_VC4_TR": [
        (0x8013, 0, 4),
    ],

    "DAC_CMP_BIAS_LSB": [
        (0x8014, 0, 8),
        (0x8015, 4, 2),
    ],
    "DAC_CMP_BIAS_LSB_TR": [
        (0x8015, 0, 4),
    ],

    "DAC_CMP_VB5": [
        (0x8016, 0, 8),
        (0x8017, 4, 2),
    ],
    "DAC_CMP_VB5_TR": [
        (0x8017, 0, 4),
    ],

    "DAC_CMP_VC5": [
        (0x8018, 0, 8),
        (0x8019, 4, 2),
    ],
    "DAC_CMP_VC5_TR": [
        (0x8019, 0, 4),
    ],

    "DAC_CMP_A": [
        (0x801A, 0, 8),
        (0x801B, 4, 2),
    ],
    "DAC_CMP_A_TR": [
        (0x801B, 0, 4),
    ],

    "DAC_CMP_B": [
        (0x801C, 0, 8),
        (0x801D, 4, 2),
    ],
    "DAC_CMP_B_TR": [
        (0x801D, 0, 4),
    ],

    "DAC_CMP_C": [
        (0x801E, 0, 8),
        (0x801F, 4, 2),
    ],
    "DAC_CMP_C_TR": [
        (0x801F, 0, 4),
    ],

    "DAC_CMP_D": [
        (0x8020, 0, 8),
        (0x8021, 4, 2),
    ],
    "DAC_CMP_D_TR": [
        (0x8021, 0, 4),
    ],

    "DAC_BUF_HB": [
        (0x8022, 0, 8),
        (0x8023, 4, 2),
    ],
    "DAC_BUF_HB_TR": [
        (0x8023, 0, 4),
    ],

    "DAC_BUF_LB": [
        (0x8024, 0, 8),
        (0x8025, 4, 2),
    ],
    "DAC_BUF_LB_TR": [
        (0x8025, 0, 4),
    ],

    "DAC_BUF50_TST": [
        (0x8026, 0, 8),
        (0x8027, 4, 2),
    ],
    "DAC_BUF50_TST_TR": [
        (0x8027, 0, 4),
    ],

    "DAC_BUF50_MUX": [
        (0x8028, 0, 8),
        (0x8029, 4, 2),
    ],
    "DAC_BUF50_MUX_TR": [
        (0x8029, 0, 4),
    ],

    "DAC_PFB": [
        (0x802A, 0, 8),
        (0x802B, 4, 2),
    ],
    "DAC_PFB_TR": [
        (0x802B, 0, 4),
    ],

    "DAC_TST_REF1": [
        (0x802C, 0, 8),
        (0x802D, 4, 2),
    ],
    "DAC_TST_REF1_TR": [
        (0x802D, 0, 4),
    ],

    "DAC_TST_REF2": [
        (0x802E, 0, 8),
        (0x802F, 4, 2),
    ],
    "DAC_TST_REF2_TR": [
        (0x802F, 0, 4),
    ],

    "TST_REF1_MUX": [
        (0x8030, 5, 1),
    ],
    "TST_REF2_MUX": [
        (0x8030, 4, 1),
    ],
    "BGR_V_MUX": [
        (0x8030, 1, 1),
    ],
    "BGR_I_MUX": [
        (0x8030, 0, 1),
    ],

    "BGR_V_TR": [
        (0x8031, 0, 8),
    ],
    "BGR_I_TR": [
        (0x8032, 0, 8),
    ],

    "TEST_MUX": [
        (0x8033, 0, 8),
        (0x8034, 0, 8),
        (0x8035, 0, 8),
        (0x8036, 0, 8),
        (0x8037, 0, 8),
    ],

    "TEST_CONF_GAIN": [
        (0x8038, 0, 5),
    ],
    "TEST_CONF_SHT": [
        (0x8038, 5, 3),
    ],
    "TEST_CONF_REG": [
        (0x8039, 0, 1),
    ],
    "TEST_CONF_SH_EN": [
        (0x8039, 1, 1),
    ],
    "TEST_CONF_TST_EN": [
        (0x8039, 2, 1),
    ],
    "TEST_CONF_BUF_NEN": [
        (0x8039, 3, 1),
    ],
    "TEST_CONF_CMPD_TR": [
        (0x8039, 4, 4),
        (0x803A, 0, 1),
    ],
    "TEST_CONF_CMPC_TR": [
        (0x803A, 1, 5),
    ],
    "TEST_CONF_CMPB_TR": [
        (0x803A, 6, 2),
        (0x803B, 0, 3),
    ],
    "TEST_CONF_CMPA_TR": [
        (0x803B, 3, 5),
    ],
}

DEFAULT_FIELD_VALUES = {
    # CSA
    "DAC_CSA_RES_FB": 150,
    "DAC_CSA_RES_FB_TR": 3,
    "DAC_CSA_VB1": 800,
    "DAC_CSA_VB1_TR": 3,
    "DAC_CSA_VC1": 384,
    "DAC_CSA_VC1_TR": 3,
    "DAC_CSA_VC2": 600,
    "DAC_CSA_VC2_TR": 3,
    "DAC_CSA_VB2": 512,
    "DAC_CSA_VB2_TR": 3,
    "DAC_CSA_VREF": 600,
    "DAC_CSA_VREF_TR": 3,

    # Shaper
    "DAC_SH_VB3": 600,
    "DAC_SH_VB3_TR": 3,
    "DAC_SH_VC3": 400,
    "DAC_SH_VC3_TR": 3,
    "DAC_SH_VB4": 350,
    "DAC_SH_VB4_TR": 3,
    "DAC_SH_VC4": 590,
    "DAC_SH_VC4_TR": 3,

    # Comparators
    "DAC_CMP_BIAS_LSB": 512,
    "DAC_CMP_BIAS_LSB_TR": 3,
    "DAC_CMP_VB5": 800,
    "DAC_CMP_VB5_TR": 3,
    "DAC_CMP_VC5": 600,
    "DAC_CMP_VC5_TR": 3,
    "DAC_CMP_A": 850,
    "DAC_CMP_A_TR": 3,
    "DAC_CMP_B": 750,
    "DAC_CMP_B_TR": 3,
    "DAC_CMP_C": 700,
    "DAC_CMP_C_TR": 3,
    "DAC_CMP_D": 650,
    "DAC_CMP_D_TR": 3,

    # Buffers
    "DAC_BUF_HB": 512,
    "DAC_BUF_HB_TR": 3,
    "DAC_BUF_LB": 512,
    "DAC_BUF_LB_TR": 3,
    "DAC_BUF50_TST": 410,
    "DAC_BUF50_TST_TR": 3,
    "DAC_BUF50_MUX": 1023,
    "DAC_BUF50_MUX_TR": 15,

    # Injection system
    "DAC_PFB": 512,
    "DAC_PFB_TR": 3,
    "DAC_TST_REF1": 600,
    "DAC_TST_REF1_TR": 3,
    "DAC_TST_REF2": 800,
    "DAC_TST_REF2_TR": 3,
    "TST_REF1_MUX": 0,
    "TST_REF2_MUX": 0,

    # Bandgap
    "BGR_V_MUX": 0,
    "BGR_I_MUX": 0,
    "BGR_V_TR": 240,
    "BGR_I_TR": 240,

    # Test mux
    "TEST_MUX": 1,

    # TEST_CONF, Table 3
    "TEST_CONF_GAIN": 4,
    "TEST_CONF_SHT": 2,
    "TEST_CONF_REG": 0,
    "TEST_CONF_SH_EN": 0,
    "TEST_CONF_TST_EN": 0,
    "TEST_CONF_BUF_NEN": 1,
    "TEST_CONF_CMPD_TR": 15,
    "TEST_CONF_CMPC_TR": 15,
    "TEST_CONF_CMPB_TR": 12,
    "TEST_CONF_CMPA_TR": 13,
}


def _pack_field(registers: dict[int, int], name: str, value: int) -> None:
    """Pack one logical field into register bytes using REGS_FIELDS."""
    fragments = REGS_FIELDS[name]
    total_bits = sum(width for _, _, width in fragments)
    max_value = (1 << total_bits) - 1

    if not (0 <= value <= max_value):
        raise ValueError(
            f"Default value {value} out of range for {name} (0..{max_value})"
        )

    remaining = value

    for address, shift, width in fragments:
        mask = ((1 << width) - 1) << shift
        part = remaining & ((1 << width) - 1)
        registers[address] = (
            (registers[address] & ~mask)
            | ((part << shift) & mask)
        )
        remaining >>= width


def _build_default_registers() -> dict[int, int]:
    registers = {
        address: 0x00
        for address in range(0x8000, 0x803C)
    }

    for name, value in DEFAULT_FIELD_VALUES.items():
        _pack_field(registers, name, value)

    return registers


DEFAULT_REGISTERS = _build_default_registers()


AMUX_MAP = {
    bit_number: (0x8033 + bit_number // 8, bit_number % 8)
    for bit_number in range(40)
}