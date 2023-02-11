import asyncio
from packaging import version
from serial import SerialException, EIGHTBITS, PARITY_NONE, STOPBITS_ONE

from mpf.core.utility_functions import Util

HEX_FORMAT = " 0x%02x"
MIN_FW = version.parse('0.00') # override in subclass
HAS_UPDATE_TASK = False

class FastSerialCommunicator:

    """Handles the serial communication to the FAST platform."""

    ignored_messages = ['WD:P']

    # __slots__ = ["aud", "dmd", "remote_processor", "remote_model", "remote_firmware", "max_messages_in_flight",
    #              "messages_in_flight", "ignored_messages_in_flight", "send_ready", "write_task", "received_msg",
    #              "send_queue", "is_retro", "is_nano", "machine", "platform", "log", "debug", "read_task",
    #              "reader", "writer"]

    def __init__(self, platform, processor, config):
        """Initialize FastSerialCommunicator."""
        self.platform = platform
        self.remote_processor = processor.upper()
        self.config = config
        self.writer = None
        self.reader = None
        self.read_task = None
        self.received_msg = b''
        self.log = platform.log  # TODO child logger per processor? Different debug logger?
        self.machine = platform.machine
        self.fast_debug = platform.debug
        self.port_debug = config['debug']

        self.remote_firmware = None  # TODO some connections have more than one processor, should there be a processor object?

        self.send_ready = asyncio.Event()
        self.send_ready.set()
        self.query_done = asyncio.Event()
        self.query_done.set()
        self.send_queue = asyncio.Queue()
        self.confirm_msg = None
        self.write_task = None

        self.ignore_decode_errors = True  # TODO set to False once the connection is established
        # TODO make this a config option? meh.
        # TODO this is not implemented yet

        self.message_processors = {'XX:': self._process_xx,
                                   'ID:': self._process_id}

    def __repr__(self):
        return f'<FAST {self.remote_processor} Communicator>'

    async def soft_reset(self):
        raise NotImplementedError

    async def connect(self):
        """Does several things to connect to the FAST processor.

        * Opens the serial connection
        * Clears out any data in the serial buffer
        * Starts the read & write tasks
        * Set the flag to ignore decode errors
        """
        self.log.debug(f"Connecting to {self.config['port']} at {self.config['baud']}bps")

        while True:
            try:
                connector = self.machine.clock.open_serial_connection(
                    url=self.config['port'], baudrate=self.config['baud'], limit=0, xonxoff=False,
                    bytesize=EIGHTBITS, parity=PARITY_NONE, stopbits=STOPBITS_ONE)
                self.reader, self.writer = await connector
            except SerialException:
                if not self.machine.options["production"]:
                    raise

                # if we are in production mode, retry
                await asyncio.sleep(.1)
                self.log.warning("Connection to %s failed. Will retry.", self.config['port'])
            else:
                # we got a connection
                break

        serial = self.writer.transport.serial
        if hasattr(serial, "set_low_latency_mode"):
            try:
                serial.set_low_latency_mode(True)
            except (NotImplementedError, ValueError) as e:
                self.log.debug(f"Could not enable low latency mode for {self.config['port']}. {e}")

        # defaults are slightly high for our use case
        self.writer.transport.set_write_buffer_limits(2048, 1024)

        # read everything which is sitting in the serial
        self.writer.transport.serial.reset_input_buffer()
        # clear buffer
        # pylint: disable-msg=protected-access
        self.reader._buffer = bytearray()

        self.ignore_decode_errors = True

        await self.clear_board_serial_buffer()

        self.ignore_decode_errors = False

        self.write_task = self.machine.clock.loop.create_task(self._socket_writer())
        self.write_task.add_done_callback(Util.raise_exceptions)

        self.read_task = self.machine.clock.loop.create_task(self._socket_reader())
        self.read_task.add_done_callback(Util.raise_exceptions)

    async def clear_board_serial_buffer(self):
        """Clear out the serial buffer."""

        self.write_to_port(b'\r\r\r\r')
        await asyncio.sleep(.5)

    async def init(self):

        await self.send_query('ID:', 'ID:')

    def _process_xx(self, msg):
        """Process the XX response."""
        self.log.warning("Received XX response: %s", msg)  # what are we going to do here? TODO

    def _process_id(self, msg):
        """Process the ID response."""
        self.remote_processor, self.remote_model, self.remote_firmware = msg.split()

        self.platform.log.info(f"Connected to {self.remote_processor} processor on {self.remote_model} with firmware v{self.remote_firmware}")

        self.machine.variables.set_machine_var("fast_{}_firmware".format(self.remote_processor.lower()),
                                               self.remote_firmware)
        '''machine_var: fast_(x)_firmware

        desc: Holds the version number of the firmware for the processor on
        the FAST Pinball controller that's connected. The "x" is replaced with
        processor attached (e.g. "net", "exp", etc).
        '''

        self.machine.variables.set_machine_var("fast_{}_model".format(self.remote_processor.lower()), self.remote_model)

        '''machine_var: fast_(x)_model

        desc: Holds the model number of the board for the processor on
        the FAST Pinball controller that's connected. The "x" is replaced with
        processor attached (e.g. "net", "exp", etc).
        '''

        if version.parse(self.remote_firmware) < MIN_FW:
            raise AssertionError(f'Firmware version mismatch. MPF requires the {self.remote_processor} processor '
                                 f'to be firmware {MIN_FW}, but yours is {self.remote_firmware}')

    async def _read_with_timeout(self, timeout):
        try:
            msg_raw = await asyncio.wait_for(self.readuntil(b'\r'), timeout=timeout)
        except asyncio.TimeoutError:
            return ""
        return msg_raw.decode()

    # pylint: disable-msg=inconsistent-return-statements
    async def readuntil(self, separator, min_chars: int = 0):
        """Read until separator.

        Args:
        ----
            separator: Read until this separator byte.
            min_chars: Minimum message length before separator
        """
        assert self.reader is not None
        # asyncio StreamReader only supports this from python 3.5.2 on
        buffer = b''
        while True:
            char = await self.reader.readexactly(1)
            buffer += char
            if char == separator and len(buffer) > min_chars:
                if self.port_debug:
                    self.log.info(f"{self.remote_processor} <<<< {buffer}")
                return buffer

    def start(self):
        """Start periodic tasks, etc.

        Called once on MPF boot, not at game start."""

    def stopping(self):
        """The serial connection is about to stop. This is called before stop() and allows you
        to do things that need to go out before the connection is closed. A 100ms delay to allow for this happens after this is called."""

    def stop(self):
        """Stop and shut down this serial connection."""
        self.log.error("Stop called on serial connection %s", self.remote_processor)
        if self.read_task:
            self.read_task.cancel()
            self.read_task = None

        if self.write_task:
            self.write_task.cancel()
            self.write_task = None

        if self.writer:
            self.writer.close()
            if hasattr(self.writer, "wait_closed"):
                # Python 3.7+ only
                self.machine.clock.loop.run_until_complete(self.writer.wait_closed())
            self.writer = None

    async def send_query(self, msg, response_msg=None):

        self.send_queue.put_nowait((f'{msg}\r'.encode(), response_msg))
        self.query_done.clear()

        await asyncio.wait_for(self.query_done.wait(), timeout=None)  # TODO should there be a timeout?

    def send_and_confirm(self, msg, confirm_msg):
        self.send_queue.put_nowait((f'{msg}\r'.encode(), confirm_msg))

    def send_blind(self, msg):
        self.send_queue.put_nowait((f'{msg}\r'.encode(), None))

    def send_bytes(self, msg):
        self.send_queue.put_nowait((msg, None))

    def _parse_msg(self, msg):
        self.received_msg += msg
        self.log.info(f'Parsing message: {msg}')

        while True:
            pos = self.received_msg.find(b'\r')

            # no more complete messages
            if pos == -1:
                break

            msg = self.received_msg[:pos]
            self.received_msg = self.received_msg[pos + 1:]

            if not msg:
                continue

            try:
                msg = msg.decode()
            except UnicodeDecodeError:
                self.log.warning(f"Interference / bad data received: {msg}")
                if not self.ignore_decode_errors:
                    raise

            if msg in self.ignored_messages:
                continue

            handled = False

            # If this message header is in our list of message processors, call it
            msg_header = msg[:3]
            if msg_header in self.message_processors:
                self.message_processors[msg_header](msg[3:])
                handled = True

            # Does this message match the start of the confirm message?
            if self.confirm_msg and msg.startswith(self.confirm_msg):
                self.confirm_msg = None
                self.send_ready.set()
                handled = True

                # Did we also have a query in progress? If so, mark it done
                if not self.query_done.is_set():
                    self.query_done.set()

            if not handled:
                self.log.warning(f"Unknown message received: {msg}")
                # TODO: should we raise an exception here?

    async def _socket_reader(self):
        while True:
            resp = await self.read(128)
            if resp is None:
                return
            self._parse_msg(resp)

    async def read(self, n=-1):
        """Read up to `n` bytes from the stream and log the result if debug is true.

        See :func:`StreamReader.read` for details about read and the `n` parameter.
        """
        try:
            resp = await self.reader.read(n)
        except asyncio.CancelledError:  # pylint: disable-msg=try-except-raise
            raise
        except Exception as e:  # pylint: disable-msg=broad-except
            self.log.warning("Serial error: {}".format(e))
            return None

        # we either got empty response (-> socket closed) or and error
        if not resp:
            self.log.warning("Serial closed.")
            self.machine.stop("Serial {} closed.".format(self.config["port"]))
            return None

        if self.port_debug:
            self.log.info(f"{self.remote_processor} <<<< {resp}")
        return resp

    async def _socket_writer(self):
        while True:
            self.log.info("Waiting for send_queue.")
            msg, confirm_msg = await self.send_queue.get()
            await asyncio.wait_for(self.send_ready.wait(), timeout=None)  # TODO timeout? Prob no, but should do something to not block forever
            self.log.info(f"Got send_queue item: {msg}, wait for: {confirm_msg}")

            try:
                self.log.info("_socket_write, is send_ready? %s", self.send_ready.is_set())
                await asyncio.wait_for(self.send_ready.wait(), timeout=1)
                self.log.info("Got send_ready.")
            except asyncio.TimeoutError:
                self.log.error("Timeout waiting for send_ready. Message was: %s", msg)
                # TODO Decide what to do here, prob raise a specific exception?
                # self.send_ready.set()  # TODO only if we decide to continue
                raise

            if confirm_msg:
                self.confirm_msg = confirm_msg
                self.send_ready.clear()

            self.write_to_port(msg)

    def write_to_port(self, msg):
        # Sends a message as is, without encoding or adding a <CR> character
        if self.port_debug:
            self.log.info(f"{self.remote_processor} >>>> {msg}")

        self.writer.write(msg)