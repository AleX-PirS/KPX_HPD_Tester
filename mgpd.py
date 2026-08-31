import logging
import socket
import time

logger = logging.getLogger(__name__)

# Default policy for GET_SHOT. False means that Python does not touch OMR
# immediately before issuing GET_SHOT and MGPDLab/UPO settings are trusted.
# It can be overridden per client instance or per get_shot() call.
CONFIGURE_OMR_BEFORE_GET_SHOT_DEFAULT = False


class MGPDClient:
    """
    Синхронный TCP-клиент для управления микросхемой через MGPDLab.

    Поддерживаемые команды:
        READ_BYTE FROM 0xAAAA
        WRITE_BYTE 0xDD TO 0xAAAA
        SET_CTRL_PIN 0|1
        SET_CTRL_PIN F=<kHz> W=<ns>
        SET_FCLK <MHz>
        SET_PIXEL_CFG ROW=<row> COL=<col> 0xXXXXXXXX
        SET_PIXEL_CFG WRITE_TO_CHIP
        GET_SHOT
        GET_PIXEL ROW <row> COL <col>

    Дополнительные chip-level helpers используют READ_BYTE/WRITE_BYTE
    и меняют только выбранные биты регистра OMR:
        set_puf_mode(0|1)
        set_win_dis_mode(0|1)
        set_polarity(0|1)
        set_mode_cnt(0|1)
        set_mode_read(0..7)
        set_crw_mode(0|1)

    При подключении может автоматически активировать KIPIX CONTROL
    записью 0xA5 по адресу 0x803C.
    """

    KIPIX_CONTROL_ADDRESS = 0x803C
    KIPIX_CONTROL_ENABLE_VALUE = 0xA5

    CTRL_PWM_MIN_FREQUENCY_KHZ = 100
    CTRL_PWM_MAX_FREQUENCY_KHZ = 50_000
    CTRL_PWM_FREQUENCY_STEP_KHZ = 10
    CTRL_PWM_WIDTH_STEP_NS = 10

    FCLK_ALLOWED_MHZ = (0, 1, 5, 10, 25, 50, 75, 100, 125, 150)
    PIXEL_MATRIX_ROWS = 32
    PIXEL_MATRIX_COLS = 32

    # Operation Mode Register (OMR[47:0]) byte addresses.
    # OMR is byte-addressed little-endian in the register map:
    #   0x0020 -> OMR[7:0]
    #   0x0021 -> OMR[15:8]
    #   0x0022 -> OMR[23:16]
    #   0x0023 -> OMR[31:24]
    OMR_BYTE_0_ADDRESS = 0x0020
    OMR_BYTE_1_ADDRESS = 0x0021
    OMR_BYTE_2_ADDRESS = 0x0022
    OMR_BYTE_3_ADDRESS = 0x0023

    OMR_MODE_READ_MASK = 0b111 << 5 # OMR[7:5]
    OMR_WIN_DIS_MODE_MASK = 1 << 1   # OMR[9]
    OMR_MODE_CNT_MASK = 1 << 2       # OMR[10]
    OMR_CRW_MODE_MASK = 1 << 3       # OMR[11]
    OMR_POL_CTRL_MASK = 1 << 3       # OMR[19]
    OMR_PUF_MODE_MASK = 1 << 4       # OMR[20]
    OMR_POL_SW_MASK = 1 << 0         # OMR[24]

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0xBEEB,
        timeout: float = 5.0,
        auto_enable_kipix: bool = True,
        configure_omr_before_get_shot: bool = CONFIGURE_OMR_BEFORE_GET_SHOT_DEFAULT,
        reconnect_attempts: int = 3,
        reconnect_backoff_s: float = 0.5,
        trace_callback=None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auto_enable_kipix = auto_enable_kipix
        self.configure_omr_before_get_shot = bool(configure_omr_before_get_shot)
        if (
            not isinstance(reconnect_attempts, int)
            or isinstance(reconnect_attempts, bool)
            or reconnect_attempts < 0
        ):
            raise ValueError("reconnect_attempts must be an integer >= 0")
        if reconnect_backoff_s < 0:
            raise ValueError("reconnect_backoff_s must be >= 0")
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_backoff_s = float(reconnect_backoff_s)
        self.trace_callback = trace_callback
        self._socket: socket.socket | None = None
        self._connected = False
        self._connect_in_progress = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self):
        """Установить TCP-соединение с MGPDLab."""
        if self._connected:
            self.disconnect()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)
        self._connect_in_progress = True

        try:
            self._socket.connect((self.host, self.port))
            self._connected = True
            logger.info("Connected to %s:%s", self.host, self.port)

            if self.auto_enable_kipix:
                if not self.enable_kipix_control():
                    raise RuntimeError(
                        "Failed to enable KIPIX CONTROL "
                        f"(write 0x{self.KIPIX_CONTROL_ENABLE_VALUE:02X} "
                        f"to 0x{self.KIPIX_CONTROL_ADDRESS:04X})"
                    )
                logger.info("KIPIX CONTROL enabled")

        except Exception as error:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            self._connected = False
            raise ConnectionError(
                f"Failed to connect or enable KIPIX: {error}"
            ) from error
        finally:
            self._connect_in_progress = False

        return self

    def disconnect(self):
        """Закрыть TCP-соединение."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None

        self._connected = False
        logger.info("Disconnected")

    def reconnect(self) -> int:
        """Reconnect to MGPDLab with bounded exponential backoff.

        The chip register state is not intentionally reset by this method.
        Successful connection again enables KIPIX CONTROL through the existing
        connection sequence.
        """

        last_error: BaseException | None = None
        for attempt in range(1, self.reconnect_attempts + 1):
            self.disconnect()
            delay = min(
                self.reconnect_backoff_s * (2 ** (attempt - 1)),
                5.0,
            )
            if delay:
                time.sleep(delay)
            try:
                self.connect()
                logger.warning(
                    "MGPDLab connection restored on attempt %d/%d",
                    attempt,
                    self.reconnect_attempts,
                )
                return attempt
            except BaseException as error:
                last_error = error
                logger.warning(
                    "MGPDLab reconnect attempt %d/%d failed: %s",
                    attempt,
                    self.reconnect_attempts,
                    error,
                )
        raise ConnectionError(
            f"MGPDLab reconnect failed after {self.reconnect_attempts} attempt(s)"
        ) from last_error

    def _send_command(self, cmd: bytes, *, retry_safe: bool = True) -> bytes:
        """Отправить одну команду и вернуть ответ MGPDLab."""
        retry_count = 0
        while True:
            if not self._connected or self._socket is None:
                raise RuntimeError("Not connected. Call connect() first.")
            try:
                if self.trace_callback is not None:
                    self.trace_callback(
                        "TX", cmd.decode("ascii", errors="replace").strip()
                    )
                self._socket.sendall(cmd)
                response = self._socket.recv(1024)
                if not response:
                    raise ConnectionError("MGPDLab closed the TCP connection")
                if self.trace_callback is not None:
                    self.trace_callback("RX", self._decode_response(response))
                return response
            except (OSError, TimeoutError, ConnectionError) as error:
                if (
                    not retry_safe
                    or self._connect_in_progress
                    or retry_count >= self.reconnect_attempts
                ):
                    raise
                retry_count += 1
                logger.warning(
                    "Transient MGPDLab transport failure; reconnecting before "
                    "safe command retry %d/%d: %s",
                    retry_count,
                    self.reconnect_attempts,
                    error,
                )
                self.reconnect()

    @staticmethod
    def _decode_response(response: bytes) -> str:
        """Декодировать текстовый ответ сервера."""
        for encoding in ("utf-8", "cp1251", "ascii"):
            try:
                return response.decode(encoding).strip()
            except UnicodeDecodeError:
                continue

        return response.decode("latin-1", errors="replace").strip()

    @classmethod
    def _check_error(cls, response: bytes) -> bool:
        """True, если сервер вернул ERROR или ОШИБКА."""
        text = cls._decode_response(response).upper()
        return "ERROR" in text or "ОШИБКА" in text

    @classmethod
    def _check_ok(cls, response: bytes) -> bool:
        """True, если ответ содержит OK и не содержит ошибку."""
        if cls._check_error(response):
            return False
        return "OK" in cls._decode_response(response).upper()

    @staticmethod
    def _validate_address(address: int):
        if not isinstance(address, int) or isinstance(address, bool):
            raise TypeError("address must be int")
        if not 0 <= address <= 0xFFFF:
            raise ValueError("address must be in range 0x0000..0xFFFF")

    @staticmethod
    def _validate_byte(value: int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("value must be int")
        if not 0 <= value <= 0xFF:
            raise ValueError("value must be in range 0x00..0xFF")

    def enable_kipix_control(self) -> bool:
        """Активировать KIPIX CONTROL."""
        return self.write_byte(
            self.KIPIX_CONTROL_ENABLE_VALUE,
            self.KIPIX_CONTROL_ADDRESS,
        )

    def write_byte(self, value: int, address: int) -> bool:
        """Записать один байт по 16-битному адресу."""
        self._validate_byte(value)
        self._validate_address(address)

        cmd = f"WRITE_BYTE 0x{value:02X} TO 0x{address:04X}\r\n".encode("ascii")
        response = self._send_command(cmd)

        if self._check_error(response):
            logger.error(
                "Write error: addr=0x%04X, val=0x%02X, response=%r",
                address,
                value,
                response,
            )
            return False

        if not self._check_ok(response):
            logger.error(
                "Unexpected WRITE_BYTE response: addr=0x%04X, val=0x%02X, response=%r",
                address,
                value,
                response,
            )
            return False

        logger.debug("Write OK: addr=0x%04X, val=0x%02X", address, value)
        return True

    def read_byte(self, address: int) -> int | None:
        """Прочитать один байт по 16-битному адресу."""
        self._validate_address(address)

        cmd = f"READ_BYTE FROM 0x{address:04X}\r\n".encode("ascii")
        response = self._send_command(cmd)

        if self._check_error(response):
            logger.error(
                "Read error: addr=0x%04X, response=%r",
                address,
                response,
            )
            return None

        response_text = self._decode_response(response)

        try:
            value = int(response_text, 16)
        except ValueError:
            logger.error("Failed to parse READ_BYTE response: %r", response_text)
            return None

        if not 0 <= value <= 0xFF:
            logger.error("READ_BYTE returned value outside byte range: %r", response_text)
            return None

        return value

    @staticmethod
    def _validate_bit_state(value: int | bool, name: str = "state") -> int:
        """Normalize a binary control value to integer 0 or 1."""
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, int) or value not in (0, 1):
            raise ValueError(f"{name} must be 0 or 1")
        return value

    def _update_byte_bits(
        self,
        address: int,
        mask: int,
        value_bits: int,
    ) -> bool:
        """Read-modify-write one byte while preserving every unmasked bit.

        No write is attempted if the read fails. If the requested bits already
        have the required value, the method returns True without issuing an
        unnecessary WRITE_BYTE.
        """
        self._validate_address(address)
        self._validate_byte(mask)
        self._validate_byte(value_bits)

        if value_bits & ~mask:
            raise ValueError("value_bits must not contain bits outside mask")

        current = self.read_byte(address)
        if current is None:
            logger.error(
                "Cannot update OMR byte: READ_BYTE failed at 0x%04X",
                address,
            )
            return False

        updated = (current & (~mask & 0xFF)) | (value_bits & mask)

        if updated == current:
            logger.debug(
                "OMR byte unchanged: addr=0x%04X, value=0x%02X",
                address,
                current,
            )
            return True

        return self.write_byte(updated, address)

    def set_puf_mode(self, state: int | bool) -> bool:
        """Set OMR[20] PUF_MODE without changing any other OMR bits.

        state:
            0 - PUF mode disabled
            1 - PUF mode enabled

        OMR[20] is bit 4 of byte address 0x0022.
        """
        state = self._validate_bit_state(state, "PUF_MODE")
        return self._update_byte_bits(
            self.OMR_BYTE_2_ADDRESS,
            self.OMR_PUF_MODE_MASK,
            self.OMR_PUF_MODE_MASK if state else 0,
        )

    def set_win_dis_mode(self, state: int | bool) -> bool:
        """Set OMR[9] WIN_DIS_MODE without changing any other OMR bits.

        state:
            0 - window discrimination disabled
            1 - window discrimination enabled

        OMR[9] is bit 1 of byte address 0x0021.
        """
        state = self._validate_bit_state(state, "WIN_DIS_MODE")
        return self._update_byte_bits(
            self.OMR_BYTE_1_ADDRESS,
            self.OMR_WIN_DIS_MODE_MASK,
            self.OMR_WIN_DIS_MODE_MASK if state else 0,
        )

    def set_mode_cnt(self, state: int | bool) -> bool:
        """Set OMR[10] MODE_CNT while preserving all unrelated OMR bits.

        state:
            0 - 16-bit counter mode
            1 - 8-bit counter mode

        OMR[10] is bit 2 of byte address 0x0021.
        """
        state = self._validate_bit_state(state, "MODE_CNT")
        return self._update_byte_bits(
            self.OMR_BYTE_1_ADDRESS,
            self.OMR_MODE_CNT_MASK,
            self.OMR_MODE_CNT_MASK if state else 0,
        )

    def set_crw_mode(self, state: int | bool) -> bool:
        """Set OMR[11] CRW_MODE while preserving all unrelated OMR bits.

        state:
            0 - sequential readout mode
            1 - continuous read/write (CRW) mode

        OMR[11] is bit 3 of byte address 0x0021.
        """
        state = self._validate_bit_state(state, "CRW_MODE")
        return self._update_byte_bits(
            self.OMR_BYTE_1_ADDRESS,
            self.OMR_CRW_MODE_MASK,
            self.OMR_CRW_MODE_MASK if state else 0,
        )

    def set_mode_read(self, mode: int) -> bool:
        """Set the 3-bit OMR[7:5] MODE_READ field.

        The raw field value 0..7 is accepted. The PMUX2 programming model
        defines the actual counter combinations for Serial/SPI operation.
        Only bits OMR[7:5] are modified.
        """
        if not isinstance(mode, int) or isinstance(mode, bool):
            raise TypeError("MODE_READ must be int")
        if not 0 <= mode <= 0b111:
            raise ValueError("MODE_READ must be in range 0..7")
        return self._update_byte_bits(
            self.OMR_BYTE_0_ADDRESS,
            self.OMR_MODE_READ_MASK,
            (mode << 5) & self.OMR_MODE_READ_MASK,
        )

    def set_polarity(self, state: int | bool) -> bool:
        """Select software polarity control and set OMR[24] POL_SW.

        The function intentionally touches only two bits:
            OMR[24] POL_SW   <- state
            OMR[19] POL_CTRL <- 1

        POL_SW is prepared first. POL_CTRL is then forced to 1 so that the
        chip uses the software-controlled OMR[24] value rather than OMR[4].
        Every byte is handled with READ_BYTE -> masked update -> WRITE_BYTE,
        preserving all unrelated OMR fields.
        """
        state = self._validate_bit_state(state, "polarity")

        # 1) Prepare OMR[24] POL_SW at address 0x0023.
        if not self._update_byte_bits(
            self.OMR_BYTE_3_ADDRESS,
            self.OMR_POL_SW_MASK,
            self.OMR_POL_SW_MASK if state else 0,
        ):
            return False

        # 2) Force OMR[19] POL_CTRL=1 at address 0x0022.
        return self._update_byte_bits(
            self.OMR_BYTE_2_ADDRESS,
            self.OMR_POL_CTRL_MASK,
            self.OMR_POL_CTRL_MASK,
        )

    @classmethod
    def _validate_pixel_coordinate(cls, row: int, col: int):
        for name, value, limit in (
            ("row", row, cls.PIXEL_MATRIX_ROWS),
            ("col", col, cls.PIXEL_MATRIX_COLS),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be int")
            if not 0 <= value < limit:
                raise ValueError(f"{name} must be in range 0..{limit - 1}")

    @staticmethod
    def _validate_uint32(value: int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("value must be int")
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("value must be in range 0x00000000..0xFFFFFFFF")

    def set_fclk(self, frequency_mhz: int) -> bool:
        """Set chip FCLK using the MGPDLab SET_FCLK command.

        Supported values are defined by MGPDLab v2.01:
            0, 1, 5, 10, 25, 50, 75, 100, 125, 150 MHz.

        A value of 0 forces the clock output low.
        """
        if not isinstance(frequency_mhz, int) or isinstance(frequency_mhz, bool):
            raise TypeError("frequency_mhz must be int")
        if frequency_mhz not in self.FCLK_ALLOWED_MHZ:
            allowed = ", ".join(str(v) for v in self.FCLK_ALLOWED_MHZ)
            raise ValueError(f"frequency_mhz must be one of [{allowed}]")

        cmd = f"SET_FCLK {frequency_mhz}\r\n".encode("ascii")
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_FCLK error: F=%d MHz, response=%r",
                frequency_mhz,
                response,
            )
            return False

        logger.debug("FCLK=%d MHz", frequency_mhz)
        return True

    def set_pixel_cfg(self, row: int, col: int, value: int) -> bool:
        """Update one pixel configuration in MGPDLab virtual memory.

        This command does NOT write the matrix to the chip. Use
        write_pixel_cfg_to_chip() for the separate commit operation.
        """
        self._validate_pixel_coordinate(row, col)
        self._validate_uint32(value)

        cmd = (
            f"SET_PIXEL_CFG ROW={row} COL={col} 0x{value:08X}\r\n"
        ).encode("ascii")
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_PIXEL_CFG error: row=%d, col=%d, value=0x%08X, response=%r",
                row,
                col,
                value,
                response,
            )
            return False

        logger.debug(
            "Pixel staged in MGPDLab: row=%d, col=%d, value=0x%08X",
            row,
            col,
            value,
        )
        return True

    def write_pixel_cfg_to_chip(self) -> bool:
        """Write MGPDLab's complete virtual pixel matrix to the chip."""
        cmd = b"SET_PIXEL_CFG WRITE_TO_CHIP\r\n"
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_PIXEL_CFG WRITE_TO_CHIP error: response=%r",
                response,
            )
            return False

        logger.debug("Pixel matrix WRITE_TO_CHIP command accepted")
        return True

    def configure_get_shot_omr(
        self,
        *,
        mode_cnt: int | bool = 0,
        mode_read: int = 0b010,
        crw_mode: int | bool = 0,
    ) -> bool:
        """Apply the OMR fields commonly required before GET_SHOT.

        Defaults follow the PMUX2 initialization/readout description:
            MODE_CNT=0  -> 16-bit counters
            MODE_READ=2 -> SPI Low + Mid + Hi counters
            CRW_MODE=0  -> sequential readout

        DCR and ICR are deliberately not modified here. Every field is updated
        through READ_BYTE -> masked modification -> WRITE_BYTE.
        """
        if not self.set_mode_cnt(mode_cnt):
            return False
        if not self.set_mode_read(mode_read):
            return False
        if not self.set_crw_mode(crw_mode):
            return False
        return True

    def get_shot(
        self,
        *,
        configure_omr: bool | None = None,
        mode_cnt: int | bool = 0,
        mode_read: int = 0b010,
        crw_mode: int | bool = 0,
    ) -> bool:
        """Execute MGPDLab GET_SHOT.

        configure_omr controls whether Python first calls
        configure_get_shot_omr(). If None, the per-client
        configure_omr_before_get_shot setting is used. The default policy is
        False, so no OMR bytes are touched unless explicitly requested.

        Note: according to the MGPDLab command description, GET_SHOT itself
        begins with the UPO "Load settings" operation. Therefore the UPO/GUI
        OMR image must be consistent with the requested shot settings; MGPDLab
        may rewrite OMR again after this optional direct pre-configuration.
        """
        if configure_omr is None:
            configure_omr = self.configure_omr_before_get_shot
        elif not isinstance(configure_omr, bool):
            raise TypeError("configure_omr must be bool or None")

        if configure_omr:
            if not self.configure_get_shot_omr(
                mode_cnt=mode_cnt,
                mode_read=mode_read,
                crw_mode=crw_mode,
            ):
                logger.error("GET_SHOT OMR pre-configuration failed")
                return False

        # GET_SHOT is not idempotent. A lost response does not prove that the
        # shutter failed to run, so the low-level transport must never resend
        # this command automatically. The characterization layer may start a
        # new explicitly recorded acquisition attempt after reconnection.
        response = self._send_command(b"GET_SHOT\r\n", retry_safe=False)
        if not self._check_ok(response):
            logger.error("GET_SHOT error: response=%r", response)
            return False

        logger.debug("GET_SHOT completed successfully")
        return True

    def get_pixel(self, row: int, col: int) -> dict[str, int | str] | None:
        """Read raw counter data for one physical pixel using GET_PIXEL.

        The returned 64-bit word is not otherwise decoded. For convenience it
        is split exactly as documented by MGPDLab:
            bits 15:0   -> low
            bits 31:16  -> mid
            bits 47:32  -> high
            bits 63:48  -> reserved (normally zero)

        The low-level command intentionally supports the complete 32x32 matrix.
        """
        self._validate_pixel_coordinate(row, col)

        cmd = f"GET_PIXEL ROW {row} COL {col}\r\n".encode("ascii")
        response = self._send_command(cmd)

        if self._check_error(response):
            logger.error(
                "GET_PIXEL error: row=%d, col=%d, response=%r",
                row,
                col,
                response,
            )
            return None

        text = self._decode_response(response).strip()
        if text.lower().startswith("0x"):
            text = text[2:]

        if not text or len(text) > 16:
            logger.error("Unexpected GET_PIXEL response: %r", response)
            return None

        try:
            raw = int(text, 16)
        except ValueError:
            logger.error("Failed to parse GET_PIXEL response: %r", response)
            return None

        if not 0 <= raw <= 0xFFFFFFFFFFFFFFFF:
            logger.error("GET_PIXEL value outside 64-bit range: %r", response)
            return None

        return {
            "row": row,
            "col": col,
            "raw": raw,
            "raw_hex": f"{raw:016X}",
            "low": raw & 0xFFFF,
            "mid": (raw >> 16) & 0xFFFF,
            "high": (raw >> 32) & 0xFFFF,
            "reserved": (raw >> 48) & 0xFFFF,
        }

    def set_ctrl(self, state: int | bool) -> bool:
        """
        Установить CTRL в статическое состояние 0 или 1.

        Пример команды MGPDLab:
            SET_CTRL_PIN 1
        """
        if isinstance(state, bool):
            state = int(state)

        if not isinstance(state, int) or state not in (0, 1):
            raise ValueError("CTRL state must be 0 or 1")

        cmd = f"SET_CTRL_PIN {state}".encode("ascii")
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_CTRL_PIN error: state=%d, response=%r",
                state,
                response,
            )
            return False

        logger.debug("CTRL=%d", state)
        return True

    def set_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> bool:
        """
        Перевести CTRL в ШИМ-режим.

        frequency_khz:
            100..50000 кГц, шаг 10 кГц.

        width_ns:
            ширина положительного импульса, шаг 10 нс.
            Должна быть не меньше 10 нс и не больше period_ns - 10 нс.
        """
        if not isinstance(frequency_khz, int) or isinstance(frequency_khz, bool):
            raise TypeError("frequency_khz must be int")
        if not isinstance(width_ns, int) or isinstance(width_ns, bool):
            raise TypeError("width_ns must be int")

        if not (
            self.CTRL_PWM_MIN_FREQUENCY_KHZ
            <= frequency_khz
            <= self.CTRL_PWM_MAX_FREQUENCY_KHZ
        ):
            raise ValueError(
                "frequency_khz must be in range "
                f"{self.CTRL_PWM_MIN_FREQUENCY_KHZ}.."
                f"{self.CTRL_PWM_MAX_FREQUENCY_KHZ}"
            )

        if frequency_khz % self.CTRL_PWM_FREQUENCY_STEP_KHZ != 0:
            raise ValueError(
                f"frequency_khz must be a multiple of "
                f"{self.CTRL_PWM_FREQUENCY_STEP_KHZ} kHz"
            )

        if width_ns < self.CTRL_PWM_WIDTH_STEP_NS:
            raise ValueError(
                f"width_ns must be >= {self.CTRL_PWM_WIDTH_STEP_NS} ns"
            )

        if width_ns % self.CTRL_PWM_WIDTH_STEP_NS != 0:
            raise ValueError(
                f"width_ns must be a multiple of "
                f"{self.CTRL_PWM_WIDTH_STEP_NS} ns"
            )

        period_ns = 1_000_000.0 / frequency_khz
        max_width_ns = period_ns - self.CTRL_PWM_WIDTH_STEP_NS

        if width_ns > max_width_ns:
            raise ValueError(
                f"width_ns={width_ns} is too large for F={frequency_khz} kHz. "
                f"Period is {period_ns:g} ns, maximum allowed width is "
                f"{max_width_ns:g} ns."
            )

        cmd = f"SET_CTRL_PIN F={frequency_khz} W={width_ns}".encode("ascii")
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_CTRL_PIN PWM error: F=%d kHz, W=%d ns, response=%r",
                frequency_khz,
                width_ns,
                response,
            )
            return False

        logger.debug("CTRL PWM: F=%d kHz, W=%d ns", frequency_khz, width_ns)
        return True

    @staticmethod
    def ctrl_pwm_real_frequency_khz(frequency_khz: int) -> float:
        """Рассчитать реальную частоту CTRL PWM по формуле из MGPDLab."""
        if frequency_khz <= 0:
            raise ValueError("frequency_khz must be positive")

        divider = 100_000 // frequency_khz
        if divider <= 0:
            raise ValueError("frequency_khz is too high")

        return 100_000 / divider

    # Alias with the protocol command terminology.
    set_ctrl_pin = set_ctrl

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
