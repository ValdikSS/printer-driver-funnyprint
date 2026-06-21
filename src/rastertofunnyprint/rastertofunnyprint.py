#!/usr/bin/python3
"""Xiqi LX-D2 and LX-D3 CUPS filter+backend (driver)

This is a driver for LX-D2, LX-D3, LX-D5 Bluetooth LE thermal
printers by Shenzhen Xiqi Technology Co. Ltd., supported by
"Funny Print" Android and iOS application.

https://play.google.com/store/apps/details?id=com.lailaixiong.funnyprint
https://apps.apple.com/us/app/funny-print/id6450447626

Other names these printer also known as:
* DOLEWA D3 Mini Printer
* DOLEWA LX-D5
* "Transform good mood" / "Have a nice day" printer

If you're familiar with 'cat printers', Xiqi/DOLEWA use completely different
protocol, which make existing 'cat' software incompatible with these models.

Xiqi printers are also incompatible with Fun Print, iBleem or WalkPrint apps.
Don't confuse Fun Print with FunNY print applications.
"""


import asyncio
import binascii
import os
import signal
import sys
from collections import namedtuple
from dataclasses import dataclass
from enum import IntEnum
from functools import partial
from itertools import islice
from struct import unpack
from urllib.parse import parse_qs, quote, urlparse

import bleak.exc
from bleak import BleakClient, BleakScanner


class CUPSRasterReaderError(Exception):
    pass


class CUPSRasterReader:
    """CUPS Raster v3 data reader.

    Based on "Writing your own CUPS printer driver in 100 lines of Python"
    https://behind.pretix.eu/2018/01/20/cups-driver/
    """
    HEADER_LEN = 1796
    CupsRas3 = namedtuple(
        # Documentation at https://www.cups.org/doc/spec-raster.html
        'CupsRas3',
        'MediaClass MediaColor MediaType OutputType AdvanceDistance AdvanceMedia Collate CutMedia Duplex HWResolutionH '
        'HWResolutionV ImagingBoundingBoxL ImagingBoundingBoxB ImagingBoundingBoxR ImagingBoundingBoxT '
        'InsertSheet Jog LeadingEdge MarginsL MarginsB ManualFeed MediaPosition MediaWeight MirrorPrint '
        'NegativePrint NumCopies Orientation OutputFaceUp PageSizeW PageSizeH Separations TraySwitch Tumble cupsWidth '
        'cupsHeight cupsMediaType cupsBitsPerColor cupsBitsPerPixel cupsBytesPerLine cupsColorOrder cupsColorSpace '
        'cupsCompression cupsRowCount cupsRowFeed cupsRowStep cupsNumColors cupsBorderlessScalingFactor cupsPageSizeW '
        'cupsPageSizeH cupsImagingBBoxL cupsImagingBBoxB cupsImagingBBoxR cupsImagingBBoxT cupsInteger0 cupsInteger1 '
        'cupsInteger2 cupsInteger3 cupsInteger4 cupsInteger5 cupsInteger6 cupsInteger7 cupsInteger8 cupsInteger9 '
        'cupsInteger10 cupsInteger11 cupsInteger12 cupsInteger13 cupsInteger14 cupsInteger15 cupsReal0 cupsReal1 '
        'cupsReal2 cupsReal3 cupsReal4 cupsReal5 cupsReal6 cupsReal7 cupsReal8 cupsReal9 cupsReal10 cupsReal11 '
        'cupsReal12 cupsReal13 cupsReal14 cupsReal15 cupsString0 cupsString1 cupsString2 cupsString3 cupsString4 '
        'cupsString5 cupsString6 cupsString7 cupsString8 cupsString9 cupsString10 cupsString11 cupsString12 cupsString13 '
        'cupsString14 cupsString15 cupsMarkerType cupsRenderingIntent cupsPageSizeName'
    )

    def __init__(self, raster_data):
        self.raster_data = raster_data
        self._reset_header_and_page()

    def _reset_header_and_page(self):
        self.current_page = 0
        self.current_page_header = None
        self.current_page_done = True

    def _read_header(self):
        if not self.raster_data:
            raise CUPSRasterReaderError('No raster data received')

        if not self.current_page:
            magic = self.raster_data.read(4)
            if (not magic or not len(magic)
                    or (magic != b'RaS3' and magic != b'3SaR')):
                raise CUPSRasterReaderError("Not a RaS3 CUPS Raster format")

        data = self.raster_data.read(self.HEADER_LEN)
        if not data:
            # No data left, finished processing
            self._reset_header_and_page()
            return False
        elif len(data) != self.HEADER_LEN:
            raise CUPSRasterReaderError("Incorrect size of header data")

        struct_data = unpack(
            '@64s 64s 64s 64s I I I I I II IIII I I I II I I I I '
            'I I I I II I I I I I I I I I I I I I I I I f ff ffff '
            'IIIIIIIIIIIIIIII ffffffffffffffff 64s 64s 64s 64s 64s'
            '64s 64s 64s 64s 64s 64s 64s 64s 64s 64s 64s 64s 64s '
            '64s',
            data
        )

        data = [
            # Strip trailing null-bytes of strings
            b.decode().rstrip('\x00') if isinstance(b, bytes) else b
            for b in struct_data
        ]
        header = self.CupsRas3._make(data)

        print("DEBUG: Got CUPS header. Width: {}, Height: {}, BytesPerLine: {}".format(
            header.cupsWidth,
            header.cupsHeight,
            header.cupsBytesPerLine
            ), file=sys.stderr)

        self.current_page += 1
        self.current_page_header = header
        self.current_page_done = False
        return header

    def page_readlines(self):
        if not self.current_page or not self.current_page_header:
            raise CUPSRasterReaderError("No current page data")

        num_lines = self.current_page_header.cupsHeight
        bytes_per_line = self.current_page_header.cupsBytesPerLine

        for _ in range(num_lines):
            line = self.raster_data.read(bytes_per_line)
            yield line

        self.current_page_done = True
        self.current_page_header = None

    def next_page(self):
        if not self.current_page_done:
            raise CUPSRasterReaderError("You're in the middle of the page data!")

        return self._read_header()


class FunnyReaderError(Exception):
    pass


class FunnyReader(CUPSRasterReader):
    """Read CUPS Raster data and convert it Xiqi format"""
    SKIP_STANDARD = 1 << 0
    SKIP_ROLL = 1 << 1
    SKIP_CUSTOM = 1 << 2

    def check_cups_ras3(self):
        if (self.current_page_header.cupsBitsPerColor != 1
                or self.current_page_header.cupsNumColors != 1):
            raise FunnyReaderError("Not 1 bit bi-level color, not supported!")

        if self.current_page_header.cupsWidth > 390:
            message = "Width {} is way too out of printing bounds!".format(
                self.current_page_header.cupsWidth)
            print("ERROR:", message, file=sys.stderr)
            raise FunnyReaderError(message)

        return True

    def check_page_size_roll(self):
        return "x999mm" in self.current_page_header.cupsPageSizeName.lower()

    def check_page_size_custom(self):
        # It seems that cupsPageSizeName has IPP standard name instead of whatever
        # is supplied as a parameter, so check job options as well.
        return ("custom" in self.current_page_header.cupsPageSizeName.lower()
                or "pagesize=custom" in sys.argv[5].lower()
                or "media=custom" in sys.argv[5].lower())

    def check_skip_blank_lines(self):
        # xiqiSkipBlankLines/Skip blank lines
        return ((self.current_page_header.AdvanceDistance & self.SKIP_ROLL
                    and self.check_page_size_roll())  # noqa
                or (self.current_page_header.AdvanceDistance & self.SKIP_CUSTOM
                    and self.check_page_size_custom())
                or (self.current_page_header.AdvanceDistance & self.SKIP_STANDARD
                    and not (self.check_page_size_roll() or self.check_page_size_custom())))

    def funny_raster_data(self, skip_blank_lines=False):
        """CUPS Raster to FunnyPrint

        This function could also skip all-white lines from the top
        and bottom of the document. This is useful for "roll" page size,
        as CUPS is not really suitable for printing on "endless" media
        and generates a very long 999mm page.

        Cut everything off in skip_blank_lines mode, print only real data.
        """
        skipped_from_top = False
        top_blank_lines = 0
        blank_lines = 0

        self.check_cups_ras3()

        iterator = self.page_readlines()
        # We need to form a single Bluetooth raster data transfer from two lines
        # of 384 pixels (48 bytes) each.
        # If lines are slightly larger (49 bytes), trim the last byte.
        while lines := tuple(islice(iterator, 2)):
            printer_line = bytearray(96)  # zero-filled
            line1 = lines[0][0:48]  # trim to 48 bytes if it's slightly more
            printer_line[0:len(line1)] = line1
            if len(lines) == 2:
                line2 = lines[1][0:48]  # trim to 48 bytes if it's slightly more
                printer_line[48:48+len(line2)] = line2

            # Regular printing mode
            if not skip_blank_lines:
                yield printer_line
                continue

            # Line skipping mode
            if printer_line == b"\x00"*96:  # blank line
                if not skipped_from_top:
                    # We haven't printed anything yet and got only blank lines
                    top_blank_lines += 1
                    continue
                # We're in the middle of the page, accumulate empty lines
                # in case this is not the end of the page yet
                blank_lines += 1
            else:
                skipped_from_top = True
                if blank_lines:
                    # print all accumulated blank lines
                    for _ in range(blank_lines):
                        yield b"\x00"*96
                    blank_lines = 0
                yield printer_line

        if skip_blank_lines:
            # Print 2 additional blank lines to prevent cropping from the bottom
            yield b"\x00"*96
        print("DEBUG: skipped", top_blank_lines*2, "top and", blank_lines*2, "bottom blank lines",
              file=sys.stderr)

    def test_cups_reader(self, skip_blank_lines=False):
        while self.current_page_header:
            data = list(self.funny_raster_data(skip_blank_lines))
            print(len(data))
            self.next_page()


class FunnyPackets:
    STATUS = b"\x5a\x02"
    HANDSHAKE_0A = b"\x5a\x0a"
    HANDSHAKE_0B = b"\x5a\x0b"
    PRINTING_PAUSED = b"\x5a\x08"
    PRINTING_FINISHED = b"\x5a\x06"
    LOST_PACKET = b"\x5a\x05"

    STATIC_CHALLENGE = b"\x00" * 10

    @staticmethod
    def hardware_info():
        return b"\x5a\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"

    @staticmethod
    def density(density_):
        """Printing density (darkness). 0-7."""
        return b"\x5a\x0c" + density_.to_bytes(1, 'big')

    @staticmethod
    def random_0a():
        """Handshake phase 1 - challenge

        Handshake involves challenge-response authentication before
        anything could be printed.

        1. The client sends client-challenge "5a 0a" packet with 10 random bytes
        2. The printer returns 10 bytes of printer-challenge in "5a 0a" reply packet
        3. The client sends challenge-response in "5a 0b" packet

        However, the protocol is completely flawed, which allows to
        just hard-code static client-challenge and generate final response
        from the MAC address only, without even using printer-challenge data.
        This is most likely not a print-challenge at all, but a garbage RAM
        data due to incorrect packet length.

        The protocol operates on byte-basis (each challenge byte corresponds to
        other response byte, regardless of other bytes or the position), that's
        why we use only the first byte out of 10, and multiply it.
        """
        return b"\x5a\x0a" + FunnyPackets.STATIC_CHALLENGE

    @staticmethod
    def reply_0b(bdaddr):
        """Handshake phase 2 - response

        The second step of pointless authentication.
        """

        def crc16_xmodem(data):
            """CRC16-XMODEM implementation matching the native application"""
            crc = 0
            for byte in data:
                for i in range(8):
                    bit = (byte >> (7 - i)) & 1
                    c15 = (crc >> 15) & 1
                    crc <<= 1
                    crc &= 0xFFFF
                    if c15 ^ bit:
                        crc ^= 0x1021
            return crc

        mac_hex = bdaddr.replace(':', '')
        payload_bytes = FunnyPackets.STATIC_CHALLENGE[0:1] + binascii.unhexlify(mac_hex)
        response = (crc16_xmodem(payload_bytes) >> 8) & 0xFF

        return b"\x5a\x0b" + bytes([response]) * 10

    @staticmethod
    def print_event(num_lines, end=False):
        """Print Start and Print Finish packets.

        num_lines are "funny" lines, i.e. two raster lines combined.
        """
        return b"\x5a\x04" + num_lines.to_bytes(2, 'big') + end.to_bytes(2, 'little')

    @staticmethod
    def print_line(line_no, data):
        """Raster data"""
        return b"\x55" + line_no.to_bytes(2, 'big') + data + b"\x00"


class FunnyBluetoothError(Exception):
    pass


@dataclass
class FunnyBluetoothControl:
    class EventType(IntEnum):
        PAUSE = 1
        LOST = 2
        FINISHED = 3

    event_type: EventType
    raw_data: bytes


class FunnyBluetooth:
    WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
    READ_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

    def __init__(self, client, bdaddr):
        self.client = client
        self.bdaddr = bdaddr
        # "Message box" for handshake process
        self.msgbox_handshake = asyncio.Queue()
        # "Message box" for handling lost packets
        self.msgbox_lost = asyncio.Queue()

    async def write(self, data):
        return await self.client.write_gatt_char(self.WRITE_UUID, data, response=False)

    async def configure(self):
        await self.client.start_notify(self.READ_UUID, partial(self._bleak_callback, self))
        print("DEBUG: Subscribed to notify channel", file=sys.stderr)

        # sendHardwareInfo
        await self.write(FunnyPackets.hardware_info())
        print("DEBUG: Sent hardwareInfo", file=sys.stderr)

        # handshake
        await self.write(FunnyPackets.random_0a())
        await self.msgbox_handshake.get()
        await self.write(FunnyPackets.reply_0b(self.bdaddr))
        handshake_result = await self.msgbox_handshake.get()
        if handshake_result[2] == 0x01:
            print("DEBUG: Handshake successful", file=sys.stderr)
        else:
            raise FunnyBluetoothError("Handshake failed")

    @staticmethod
    async def _bleak_callback(self, sender, data):
        """Notify channel callback for bleak

        You wonder why I used staticmethod here?
        Bleak sends only (sender, data) to the callback function, without any
        way to pass auxiliary data, that's why it could not be a class/instance
        method, making FunnyBluetooth unable to be instanced twice or
        more times—the callback won't know which class it belongs and will
        potentially rewrite class data.

        However, despite what's stated in Bleak documentation (and what's
        really implemented in the code), somehow regular instance method
        with (self, sender, data) works absolutely fine, with proper self
        as FunnyBluetooth.
        Apparently, that's some Python's generator-wrapping magic which injects
        self to async function.

        I decided to play safe and wrap the function with functools.partial,
        injecting (self) myself, and converting it to staticmethod.
        I'm not sure if the former method will work on all Python versions, as
        it feels like a Python implementation detail.
        """
        print("DEBUG: Funny notification data",
              binascii.hexlify(data).decode(), file=sys.stderr)

        packet_type = data[0:2]

        if packet_type == FunnyPackets.HANDSHAKE_0A:    # handshake step 1
            self.msgbox_handshake.put_nowait(data)

        elif packet_type == FunnyPackets.HANDSHAKE_0B:  # handshake step 2
            self.msgbox_handshake.put_nowait(data)

        elif packet_type == FunnyPackets.LOST_PACKET:
            # print("Packet lost!")
            event = FunnyBluetoothControl(
                event_type=FunnyBluetoothControl.EventType.LOST,
                raw_data=data)
            self.msgbox_lost.put_nowait(event)

        elif packet_type == FunnyPackets.PRINTING_FINISHED:
            event = FunnyBluetoothControl(
                event_type=FunnyBluetoothControl.EventType.FINISHED,
                raw_data=data)
            self.msgbox_lost.put_nowait(event)

        elif packet_type == FunnyPackets.PRINTING_PAUSED:
            print("DEBUG: Printing is paused by the printer", file=sys.stderr)
            event = FunnyBluetoothControl(
                event_type=FunnyBluetoothControl.EventType.PAUSE,
                raw_data=data)
            self.msgbox_lost.put_nowait(event)

        elif packet_type == FunnyPackets.STATUS:  # battery and other data
            # battery_level = data[2]
            no_paper = data[3]
            # charge = data[4]
            overheat = data[5]
            # lowVoltage = data[6]
            # density = data[7]
            print("DEBUG: Battery {}% ({})".format(data[2], binascii.hexlify(data).decode()), file=sys.stderr)
            print("STATE: {}fuser-over-temp".format("+" if overheat else "-"), file=sys.stderr)
            print("STATE: {}media-empty".format("+" if no_paper else "-"), file=sys.stderr)
            if overheat:
                print("WARNING: printer is overheating", file=sys.stderr)


async def main():
    if len(sys.argv) == 7:
        pagedata = open(sys.argv[6], "rb")
    elif len(sys.argv) == 6:
        pagedata = sys.stdin.buffer
    else:
        print("Xiqi FunnyPrint Bluetooth thermal printer", file=sys.stderr)
        print("This is CUPS backend/filter, do not call directly", file=sys.stderr)
        print("Usage: {} job-id user title copies options [file]".format(sys.argv[0]), file=sys.stderr)
        return 1

    try:
        # CUPS puts DEVICE_URI to the filter/backend environment variable
        # funnyprint://Xiqi/LX-D02?address=C0%3A00%3A00%3A00%3A05%3ABE
        device_uri = os.getenv("DEVICE_URI")
        device_uri = urlparse(device_uri)
        bdaddr = parse_qs(device_uri.query)['address'][0]
    except (ValueError, KeyError) as e:
        print("ERROR: DEVICE_URI error:", repr(e), file=sys.stderr)
        return 1

    cupsreader = FunnyReader(pagedata)
    if not cupsreader.next_page():
        raise FunnyReaderError("Invalid CUPS Raster data!")

    # Printing darkness
    density = cupsreader.current_page_header.cupsCompression

    skip_lines = cupsreader.check_skip_blank_lines()
    print("DEBUG: Skip blank lines mode {}enabled for {}".format(
        "" if skip_lines else "NOT ",
        cupsreader.current_page_header.cupsPageSizeName), file=sys.stderr)

    # This is left for testing
    # cupsreader.test_cups_reader(skip_blank_lines=True)
    # return 1

    print("STATE: -timed-out", file=sys.stderr)
    print("STATE: +connecting-to-device", file=sys.stderr)

    try:
        async with BleakClient(bdaddr) as client:
            funny = FunnyBluetooth(client, bdaddr)
            await funny.configure()
            print("STATE: -connecting-to-device", file=sys.stderr)

            await funny.write(FunnyPackets.density(density))

            while cupsreader.current_page_header:
                data = list(cupsreader.funny_raster_data(skip_blank_lines=skip_lines))
                page_height = len(data)

                print("PAGE:", cupsreader.current_page, 1, file=sys.stderr)
                # start printing, num lines
                await funny.write(FunnyPackets.print_event(page_height, end=False))

                cur_line = 0
                wait_for_event_cnt = 0
                while True:
                    if not funny.msgbox_lost.empty():
                        event = await funny.msgbox_lost.get()
                        if event.event_type == FunnyBluetoothControl.EventType.LOST:  # Packet loss
                            wait_for_event_cnt = 0
                            # In case of "lost packet" event, we need to retransmit packets starting from
                            # the lost_packet - 1, that's what the official app does
                            cur_line = int.from_bytes(event.raw_data[2:4], 'big') - 1
                            print("DEBUG: lost packets, starting from line {} again".format(cur_line), file=sys.stderr)

                        elif event.event_type == FunnyBluetoothControl.EventType.PAUSE:  # Pause printing
                            print("DEBUG: paused, waiting for lost packet event", file=sys.stderr)
                            # Pause printing and wait for further events
                            while funny.msgbox_lost.empty():
                                await asyncio.sleep(0.1)
                            continue

                        elif event.event_type == FunnyBluetoothControl.EventType.FINISHED:  # printing finished
                            break

                    if cur_line < len(data):
                        # Printing data
                        data_line = data[cur_line]
                        # print("DEBUG: printing", cur_line, file=sys.stderr)
                        await funny.write(FunnyPackets.print_line(cur_line, data_line))
                        await asyncio.sleep(0.02)  # about 50 pkt/s
                        cur_line += 1

                    if cur_line >= len(data):
                        print("DEBUG: waiting for final confirmation or possible lost packets", file=sys.stderr)
                        # wait for "print ok" packet
                        if wait_for_event_cnt > 50:
                            break
                        if not funny.msgbox_lost.empty():
                            continue
                        wait_for_event_cnt += 1
                        await asyncio.sleep(0.5)

                # ended printing
                await funny.write(FunnyPackets.print_event(len(data), end=True))
                # load next page in raster data
                cupsreader.next_page()

    except bleak.exc.BleakDeviceNotFoundError:
        print("STATE: -connecting-to-device", file=sys.stderr)
        print("STATE: +timed-out", file=sys.stderr)
        return 1


async def main_scan():
    """Bluetooth scanning mode

    Xiqi devices don't have identifying service UUIDs, but they
    include bogus "manufacturer data" in the advertisement, with their
    MAC address.

    Use this as identification method.
    """
    try:
        devices = await BleakScanner.discover(timeout=2, return_adv=True)
    except bleak.exc.BleakError as e:
        print("ERROR:", repr(e), file=sys.stderr)
        return 1

    for device in devices:
        dev = devices[device]
        bdaddr, adv_data = dev
        print("DEBUG: found BLE device", bdaddr, file=sys.stderr)
        if adv_data.service_data or not adv_data.local_name:
            continue
        if adv_data.service_uuids and '0000ffe6-0000-1000-8000-00805f9b34fb' not in adv_data.service_uuids:
            continue

        man_data = adv_data.manufacturer_data
        if (len(man_data) == 1
                and len(b"".join(man_data.values())) == 4
                and bdaddr.name.isascii()):
            # Construct CUPS backend device information
            print('network funnyprint://{make}/{model}?address={bdaddr} "{makemodel}" "{makemodel}(FunnyPrint)" "{ieeeid}" ""'.format(
                make="Xiqi",
                model=quote(bdaddr.name),
                makemodel="Xiqi " + bdaddr.name.strip().replace('"', ""),
                bdaddr=quote(bdaddr.address),
                ieeeid="MFG:Xiqi;MDL:" + bdaddr.name.strip().replace('"', "") + ";CMD:funnyprint;"
                ))


def cli():
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    try:
        if len(sys.argv) == 1:
            # Scanning mode
            return asyncio.run(main_scan())

        return asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        return 130
