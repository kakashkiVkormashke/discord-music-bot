import asyncio
import logging
import os
import re
import shutil
from urllib.parse import urlparse

import discord
import yt_dlp
from dotenv import load_dotenv

load_dotenv()
CHANNEL_ID = int(os.getenv('TEXT_CHANNEL_ID', '1429229290540503061'))
log = logging.getLogger('music')


def parse(text):
    text = text.strip()
    normalized = ' '.join(text.lower().replace('ё', 'е').split()).rstrip('!.?')
    commands = {
        'skip': {'следующий', 'включи следующий', 'следующий трек', 'пропусти', 'скип'},
        'stop': {'стоп', 'останови музыку', 'останови', 'выключи музыку'},
        'pause': {'пауза', 'поставь на паузу'},
        'resume': {'продолжи', 'продолжи музыку', 'сними с паузы'},
        'queue': {'очередь', 'покажи очередь'},
    }
    for action, phrases in commands.items():
        if normalized in phrases:
            return action, ''
    query = re.sub(r'^(включи|поставь|найди)\s+', '', text, flags=re.I).strip()
    return 'play', query


def extract(query):
    if '://' in query:
        url = urlparse(query)
        if url.scheme not in {'https', 'http'} or url.hostname not in {
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be',
        }:
            raise ValueError('Нужна ссылка YouTube или название трека.')
        target = query
    else:
        target = 'ytsearch1:' + query
    with yt_dlp.YoutubeDL({
        'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True,
        'socket_timeout': 20, 'retries': 2, 'extractor_retries': 2,
        'js_runtimes': {'deno': {}},
    }) as ydl:
        info = ydl.extract_info(target, download=False)
        if info and 'entries' in info:
            info = next((entry for entry in info['entries'] if entry), None)
        if not info or not info.get('url'):
            raise ValueError('Ничего не найдено. Попробуй другое название.')
        return info


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.voice_states = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.queue = asyncio.Queue(maxsize=50)
        self.lock = asyncio.Lock()
        self.voice = None
        self.worker = None
        self.generation = 0
        self.current = None

    async def setup_hook(self):
        self.worker = asyncio.create_task(self.player())

    async def say(self, channel, text):
        try:
            await channel.send(text[:1900])
        except discord.HTTPException:
            log.warning('Could not send status message')

    async def on_ready(self):
        log.info('Connected as %s; channel=%s', self.user, CHANNEL_ID)

    def clear_queue(self):
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    async def on_message(self, message):
        if message.author.bot or not message.guild or message.channel.id != CHANNEL_ID:
            return
        if not message.content.strip():
            return
        action, query = parse(message.content)
        async with self.lock:
            member_voice = getattr(message.author, 'voice', None)
            target = member_voice.channel if member_voice else None
            if not isinstance(target, discord.VoiceChannel):
                await self.say(message.channel, 'Сначала зайди в обычный голосовой канал.')
                return
            vc = message.guild.voice_client
            if vc and vc.channel != target:
                await self.say(message.channel, 'Зайди в мой голосовой канал, чтобы заказать трек или управлять музыкой.')
                return
            self.voice = vc
            if action == 'queue':
                upcoming = [item[1] for item in list(self.queue._queue)]
                await self.say(message.channel, 'Сейчас: ' + (self.current or 'тишина') + '\nОчередь:\n' + ('\n'.join(upcoming[:15]) or 'пуста'))
                return
            if action == 'stop':
                self.generation += 1
                self.clear_queue()
                self.current = None
                if vc:
                    vc.stop()
                    await vc.disconnect()
                self.voice = None
                await self.say(message.channel, 'Остановлено, очередь очищена.')
                return
            if action == 'skip':
                self.generation += 1
                if vc:
                    vc.stop()
                await self.say(message.channel, 'Перехожу к следующему треку.')
                return
            if action in {'pause', 'resume'}:
                if vc and action == 'pause' and vc.is_playing():
                    vc.pause()
                    await self.say(message.channel, 'Пауза.')
                elif vc and action == 'resume' and vc.is_paused():
                    vc.resume()
                    await self.say(message.channel, 'Продолжаю.')
                else:
                    await self.say(message.channel, 'Сейчас нет трека для этого действия.')
                return
            if not query or len(query) > 500:
                await self.say(message.channel, 'Напиши название или ссылку YouTube длиной до 500 символов.')
                return
            if self.queue.full():
                await self.say(message.channel, 'Очередь заполнена (50 запросов).')
                return
            if not vc or not vc.is_connected():
                try:
                    self.voice = await target.connect(timeout=30, self_deaf=True)
                except (discord.DiscordException, asyncio.TimeoutError):
                    await self.say(message.channel, 'Не удалось подключиться. Проверь мои права Connect и Speak.')
                    return
            self.queue.put_nowait((message.channel, query))
            await self.say(message.channel, 'Добавлено: ' + discord.utils.escape_markdown(query))

    async def player(self):
        while True:
            channel, query = await self.queue.get()
            version = self.generation
            vc = self.voice
            self.current = query
            source = None
            try:
                info = await asyncio.to_thread(extract, query)
                async with self.lock:
                    if version != self.generation or vc is not self.voice or not vc or not vc.is_connected():
                        continue
                    done = asyncio.get_running_loop().create_future()
                    loop = asyncio.get_running_loop()

                    def finish(error):
                        if not done.done():
                            done.set_result(error)

                    source = discord.FFmpegPCMAudio(
                        info['url'],
                        before_options='-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000',
                        options='-vn',
                    )
                    vc.play(source, after=lambda error: loop.call_soon_threadsafe(finish, error))
                    source = None  # VoiceClient owns cleanup after play succeeds.
                    self.current = info.get('title', query)
                await self.say(channel, 'Играет: ' + discord.utils.escape_markdown(self.current or query))
                error = await done
                if error:
                    await self.say(channel, 'Поток прервался. Перехожу к следующему запросу.')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Playback failed: %s', type(error).__name__)
                if version == self.generation:
                    await self.say(channel, 'Не удалось воспроизвести запрос. Попробуй другое название или ссылку; если повторяется — проверь журнал сервера и обнови yt-dlp.')
            finally:
                if source:
                    source.cleanup()
                self.current = None
                self.queue.task_done()

    async def on_voice_state_update(self, member, before, after):
        if member.id == self.user.id and before.channel and after.channel is None:
            self.generation += 1
            self.clear_queue()
            if self.voice:
                self.voice.stop()
            self.voice = None

    async def close(self):
        if self.worker:
            self.worker.cancel()
        for vc in self.voice_clients:
            await vc.disconnect(force=True)
        if self.worker:
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().close()


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in environment or .env')
    for binary in ('ffmpeg', 'deno'):
        if not shutil.which(binary):
            raise SystemExit(f'Install {binary} and add it to PATH')
    MusicBot().run(token)
