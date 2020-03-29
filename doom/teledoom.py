#!/usr/bin/env python3

import os, re, signal, sys
from enum import Enum
import vizdoom as vzd
import numpy as np
import phonenumbers
from PIL import Image, ImageDraw, ImageFont
import ffmpeg
import aripy3
import asyncio, aiohttp
import json
import logging

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

SIMWOOD_API_USER = os.getenv('SIMWOOD_API_USER')
SIMWOOD_API_PASSWORD = os.getenv('SIMWOOD_API_PASSWORD')
SIMWOOD_ACCOUNT = os.getenv('SIMWOOD_ACCOUNT')
SIMWOOD_NUMBER = os.getenv('SIMWOOD_NUMBER')
DOOM_FPS = int(os.getenv('DOOM_FPS', '35'))
TWITCH_CBR = os.getenv('TWITCH_CBR', '100k')
TWITCH_URL = os.getenv('TWITCH_URL')
if not TWITCH_URL:
    log.error("TWITCH_URL environment variable must be set with form rtmp://{location}.twitch.tv/app/{stream_key}")
    log.error("See https://stream.twitch.tv/ingests/ for locations")
    log.error("See https://www.twitch.tv/broadcast/dashboard/streamkey for your stream key")
    sys.exit(1)

class Overlay:
    def __init__(self):
        self.font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 16)
        self.set_caller(None)

    def set_caller(self, phone_number):
        if not phone_number:
            new_caller = "No caller"
        else:
            new_caller = Overlay.format_phone_number(phone_number)
        self.caller = new_caller

    @staticmethod
    def format_phone_number(phone_number):
        try:
            # Parse and format the number
            phone_number = phonenumbers.parse(phone_number, None)
            phone_number = phonenumbers.format_number(phone_number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)

            # Obfuscate by finding the middle 4 digits and replacing them with Xs
            num_digits = len(re.findall('\d', phone_number))
            num_tail_digits = (num_digits - 4) // 2
            tail_digits = re.search('([^0-9]*[0-9]){' + str(max(num_tail_digits, 0)) + '}$', phone_number).group()
            num_head_digits = num_digits - 4 - num_tail_digits
            head_digits = re.search('^([^0-9]*[0-9]){' + str(max(num_head_digits, 0)) + '}', phone_number).group()
            middle_digits = phone_number[len(head_digits):-len(tail_digits)]
            middle_digits = re.sub('\d', 'X', middle_digits)
            phone_number = head_digits + middle_digits + tail_digits
        except phonenumbers.NumberParseException:
            log.exception("phonenumbers.NumberParseException while parsing '" + phone_number + "'")
            phone_number = "Unknown caller"
        return phone_number

    def draw(self, frame):
        frame = Image.fromarray(frame)
        draw = ImageDraw.Draw(frame)
        width = draw.textsize(self.caller, self.font)[0]
        draw.polygon([(320-width-12, 0), (320, 0), (320, 20), (320-width-2, 20)], fill=(128, 0, 0, 255), outline=(0, 0, 0, 255))
        draw.text((320-width-1,1), self.caller, font=self.font, fill=(255,255,255,255))
        del draw
        frame = np.array(frame)
        return frame

class TwitchStream:
    def __init__(self, ffmpeg):
        self.ffmpeg = ffmpeg

    def send_frame(self, frame):
        self.ffmpeg.stdin.write(frame.astype(np.uint8).tobytes())

class Twitch:
    def __init__(self, url, fps, cbr, verbose=False):
        self.url = url
        self.fps = fps
        self.cbr = cbr
        self.verbose = verbose

    def __enter__(self):
        self.ffmpeg = ffmpeg.output(
            ffmpeg.input(
                'anullsrc=channel_layout=stereo:sample_rate=44100',
                format='lavfi'
            ),
            ffmpeg.input(
                'pipe:', format='rawvideo',
                pix_fmt='rgb24',
                r=DOOM_FPS,
                s='320x240'
            ),
            TWITCH_URL,
            shortest=None,
            format='flv',
            vcodec='libx264',
            g=2*DOOM_FPS,
            keyint_min=DOOM_FPS,
            pix_fmt='yuv420p',
            preset='ultrafast',
            tune='zerolatency',
            threads=1,
            acodec="aac",
            video_bitrate=TWITCH_CBR,
            minrate=TWITCH_CBR,
            maxrate=TWITCH_CBR,
            fflags='nobuffer',
            probesize=32,
            analyzeduration=0
        ).run_async(pipe_stdin=True, quiet=not self.verbose)
        return TwitchStream(self.ffmpeg)

    def __exit__(self, type, value, traceback):
        self.ffmpeg.stdin.close()
        self.ffmpeg.wait()

class Event(Enum):
    GOT_CONNECTION = 1,
    NEW_PLAYER = 2,
    NO_PLAYER = 3,
    BUTTON_PRESSED = 4,

class Simwood:
    BASE_URL = 'https://api.simwood.com/v3/'

    def __init__(self, api_user, api_password, account, number):
        self.auth = aiohttp.BasicAuth(api_user, api_password)
        self.account = account
        self.number = number

    async def send_sms(self, to, message):
        async with aiohttp.ClientSession(auth=self.auth) as session:
            url = Simwood.BASE_URL + 'messaging/' + self.account + '/sms'
            try:
                to = phonenumbers.parse(to, None)
                to = phonenumbers.format_number(to, phonenumbers.PhoneNumberFormat.E164)
                data = {'from': self.number,
                        'to': to,
                        'message': message}
                data = json.dumps(data)
                log.info('Attempting to send SMS: ' + data)
                async with session.post(url, data=data) as response:
                    log.info('SMS send attempt returned: ' + await response.text())
            except:
                log.exception("phonenumbers.NumberParseException while parsing '" + to + "' - SMS not sent")

class Asterisk:
    def __init__(self, queue, simwood=None):
        self.doom_queue = queue
        self.simwood = simwood
        self.playing = None
        self.waiting = []

    async def start(self):
        ari = await aripy3.connect('http://localhost:8088/', 'asterisk', 'asterisk')
        await ari.on_channel_event('StasisStart', self.on_start)
        await ari.run(apps="teledoom")

    async def on_start(self, channel, event):
        await self.doom_queue.put((Event.GOT_CONNECTION, None))
        channel = channel['channel']
        caller = channel.json['caller']['number']
        await channel.on_event('ChannelDtmfReceived', self.on_dtmf)
        await channel.on_event('StasisEnd', self.on_end)
        await channel.answer()
        if simwood:
            await simwood.send_sms(caller, "Welcome to TeleDoom!  Please go to https://twitch.tv/teledoom to view the action.")
        await asyncio.sleep(0.5)
        await channel.play(media='sound:welcome-to-teledoom')
        await asyncio.sleep(1.0)
        await channel.play(media='sound:please-go-to-twitch')
        await asyncio.sleep(1.0)
        if not self.playing:
            self.playing = (caller, channel.id)
            await channel.play(media='sound:you-are-entering-the-game')
            await self.doom_queue.put((Event.NEW_PLAYER, self.playing[0]))
        else:
            self.waiting.append((caller, channel.id))
            await channel.play(media='sound:you-are-being-placed-in-a-queue')

    async def on_dtmf(self, channel, event):
        if self.playing and channel.id == self.playing[1]:
            await self.doom_queue.put((Event.BUTTON_PRESSED, event['digit']))

    async def on_end(self, channel, event):
        if channel.id == self.playing[1]:
            if len(self.waiting) > 0:
                self.playing = self.waiting[0]
                self.waiting = self.waiting[1:]
                await channel.play(media='sound:you-are-entering-the-game')
                await self.doom_queue.put((Event.NEW_PLAYER, self.playing[0]))
            else:
                self.playing = None
                await self.doom_queue.put((Event.NO_PLAYER, None))
        else:
            self.waiting = [x for x in self.waiting if channel.id != x[1]]

class ButtonManager:
    BUTTON_MAP = {
        '1': vzd.Button.MOVE_LEFT,          '2': vzd.Button.MOVE_FORWARD,  '3': vzd.Button.MOVE_RIGHT,
        '4': vzd.Button.TURN_LEFT,          '5': vzd.Button.ATTACK,        '6': vzd.Button.TURN_RIGHT,
        '7': vzd.Button.CROUCH,             '8': vzd.Button.MOVE_BACKWARD, '9': vzd.Button.JUMP,
        '*': vzd.Button.SELECT_PREV_WEAPON, '0': vzd.Button.USE,           '#': vzd.Button.SELECT_NEXT_WEAPON,
    }
    BUTTON_LIST = list(BUTTON_MAP.values())

    def __init__(self):
        self.button_timeout = [0 for _ in ButtonManager.BUTTON_LIST]

    def button_pressed(self, button, timeout):
        if button in ButtonManager.BUTTON_MAP:
            button = ButtonManager.BUTTON_MAP[button]
            self.button_timeout[ButtonManager.BUTTON_LIST.index(button)] = timeout
        else:
            log.warning("Button '" + button + "' not found in BUTTON_MAP - ignoring")

    def get_action(self):
        self.button_timeout = [max(x - 1, 0) for x in self.button_timeout]
        return [x > 0 for x in self.button_timeout]

class Doom:
    def __init__(self, loop, twitch, queue):
        self.loop = loop
        self.twitch = twitch
        self.asterisk_queue = queue
        self.overlay = Overlay()

        self.game = vzd.DoomGame()
        self.game.set_mode(vzd.Mode.PLAYER)
        self.game.set_screen_resolution(vzd.ScreenResolution.RES_320X240)
        self.game.set_screen_format(vzd.ScreenFormat.RGB24)
        self.game.set_window_visible(False)
        self.game.set_render_hud(True)
        self.game.set_available_buttons(ButtonManager.BUTTON_LIST)
        self.game.set_episode_timeout(0)
        self.game.set_ticrate(DOOM_FPS)

    async def start(self):
        self.game.init()

        idle = True
        idle_frames_left = 0
        try:
            while True:
                while True:
                    try:
                        self.game.advance_action()
                        event = await asyncio.wait_for(self.asterisk_queue.get(), 1 / DOOM_FPS)
                        break
                    except asyncio.exceptions.TimeoutError:
                        pass
                event = await self.asterisk_queue.get()
                log.info(event)
                if event[0] == Event.GOT_CONNECTION:
                    idle_frames_left = DOOM_FPS * 15
                elif event[0] == Event.NEW_PLAYER:
                    self.overlay.set_caller(event[1])
                    idle = False
                elif event[0] == Event.NO_PLAYER:
                    self.overlay.set_caller(None)
                    idle_frames_left = DOOM_FPS * 15

                with self.twitch as stream:
                    start = self.loop.time()
                    frames = 0
                    self.game.new_episode()
                    button_manager = ButtonManager()
                    while not idle or idle_frames_left > 0:
                        if self.game.is_episode_finished():
                            self.game.new_episode()
                            button_manager = ButtonManager()
                        state = self.game.get_state()
                        frame = state.screen_buffer

                        frame = self.overlay.draw(frame)

                        stream.send_frame(frame)

                        frames += 1
                        if idle:
                            idle_frames_left -= 1
                        try:
                            while True:
                                event = await asyncio.wait_for(self.asterisk_queue.get(), timeout=start + frames / DOOM_FPS - self.loop.time())
                                log.info(event)
                                if event[0] == Event.GOT_CONNECTION:
                                    if idle:
                                        idle_frames_left = DOOM_FPS * 15
                                elif event[0] == Event.NEW_PLAYER:
                                    self.overlay.set_caller(event[1])
                                    self.game.new_episode()
                                    button_manager = ButtonManager()
                                    idle = False
                                elif event[0] == Event.NO_PLAYER:
                                    self.overlay.set_caller(None)
                                    self.game.new_episode()
                                    button_manager = ButtonManager()
                                    idle = True
                                    idle_frames_left = DOOM_FPS * 15
                                elif event[0] == Event.BUTTON_PRESSED:
                                    button_manager.button_pressed(event[1], DOOM_FPS // 2)
                        except asyncio.exceptions.TimeoutError:
                            pass
                        self.game.make_action(button_manager.get_action())
        except:
            log.exception("Caught exception - stopping")
            self.loop.stop()
        finally:
            try:
                self.game.close()
            except vzd.SignalException:
                log.exception("Caught vzd.SignalException while closing")

if SIMWOOD_API_USER and SIMWOOD_API_PASSWORD and SIMWOOD_ACCOUNT and SIMWOOD_NUMBER:
    simwood = Simwood(SIMWOOD_API_USER, SIMWOOD_API_PASSWORD, SIMWOOD_ACCOUNT, SIMWOOD_NUMBER)
else:
    log.warning("SIMWOOD_API_USER, SIMWOOD_API_PASSWORD, SIMWOOD_ACCOUNT and SIMWOOD_NUMBER not specified - no SMS integration!")
    simwood = None
twitch = Twitch(TWITCH_URL, DOOM_FPS, TWITCH_CBR, verbose=True)
queue = asyncio.Queue()
loop = asyncio.get_event_loop()
for sig in [signal.SIGHUP, signal.SIGTERM, signal.SIGINT]:
    loop.add_signal_handler(sig, lambda: loop.stop())
try:
    loop.create_task(Asterisk(queue, simwood).start())
    loop.create_task(Doom(loop, twitch, queue).start())
    loop.run_forever()
finally:
    loop.close()

