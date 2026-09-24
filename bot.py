import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import discord
import yt_dlp
from dotenv import load_dotenv

load_dotenv()
DEFAULT_CHANNEL_ID = int(os.getenv('TEXT_CHANNEL_ID', '1429229290540503061'))
CHANNEL_CONFIG_PATH = Path(os.getenv('CHANNEL_CONFIG_PATH', 'channels.json'))
CHANNEL_NAME_SUFFIX = os.getenv('CHANNEL_NAME_SUFFIX', '_ms').strip().lower() or '_ms'
PLAYLIST_BATCH_SIZE = max(1, min(int(os.getenv('PLAYLIST_BATCH_SIZE', '25')), 50))
PLAYLIST_MAX_ITEMS = max(PLAYLIST_BATCH_SIZE, int(os.getenv('PLAYLIST_MAX_ITEMS', '300')))
QUEUE_MAX_SIZE = 50
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
        'playlist_next': {'дальше', 'еще', 'ещё', 'следующие', 'следующая пачка', 'загрузи дальше', 'докинь плейлист'},
    }
    for action, phrases in commands.items():
        if normalized in phrases:
            return action, ''
    query = re.sub(r'^(включи|поставь|найди)\s+', '', text, flags=re.I).strip()
    return 'play', query


def is_url(text):
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def ydl_options(**extra):
    options = {
        'quiet': True,
        'socket_timeout': 20,
        'retries': 2,
        'extractor_retries': 2,
        'js_runtimes': {'deno': {}},
    }
    options.update(extra)
    return options


def extract(query):
    if is_url(query):
        target = query
    else:
        target = 'ytsearch1:' + query
    with yt_dlp.YoutubeDL(ydl_options(format='bestaudio/best', noplaylist=True)) as ydl:
        info = ydl.extract_info(target, download=False)
        if info and 'entries' in info:
            info = next((entry for entry in info['entries'] if entry), None)
        if not info or not info.get('url'):
            raise ValueError('Ничего не найдено. Попробуй другое название или ссылку.')
        return info


def entry_to_query(entry):
    webpage_url = entry.get('webpage_url')
    if webpage_url:
        return webpage_url
    url = entry.get('url')
    if url and is_url(url):
        return url
    if entry.get('ie_key') == 'Youtube' and entry.get('id'):
        return 'https://www.youtube.com/watch?v=' + entry['id']
    if url:
        return url
    title = entry.get('title')
    if title:
        return title
    return None


def extract_playlist(query):
    if not is_url(query):
        return None
    with yt_dlp.YoutubeDL(ydl_options(extract_flat=True, noplaylist=False, playlistend=PLAYLIST_MAX_ITEMS)) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = [entry for entry in (info or {}).get('entries') or [] if entry]
    if not entries:
        return None
    items = []
    seen = set()
    for entry in entries[:PLAYLIST_MAX_ITEMS]:
        item_query = entry_to_query(entry)
        if not item_query or item_query in seen:
            continue
        seen.add(item_query)
        title = entry.get('title') or item_query
        items.append((item_query, title))
    if not items:
        return None
    title = (info or {}).get('title') or 'плейлист'
    return {'title': title, 'items': items}


def channel_config_candidates():
    candidates = [CHANNEL_CONFIG_PATH]
    if not CHANNEL_CONFIG_PATH.is_absolute():
        candidates.append(Path('/tmp') / CHANNEL_CONFIG_PATH.name)
    else:
        candidates.append(Path('/tmp/channels.json'))
    unique = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


@dataclass
class PlaylistSession:
    title: str
    items: list[tuple[str, str]]
    offset: int = 0


@dataclass
class GuildState:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAX_SIZE))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    voice: discord.VoiceClient | None = None
    worker: asyncio.Task | None = None
    generation: int = 0
    current: str | None = None
    playlist: PlaylistSession | None = None


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.voice_states = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.states = {}
        self.channels = self.load_channels()

    def load_channels(self):
        for path in channel_config_candidates():
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                log.info('Loaded channel config from %s', path)
                return {int(guild_id): int(channel_id) for guild_id, channel_id in data.items()}
            except (OSError, ValueError, TypeError):
                log.warning('Could not read %s; trying next channel config path', path)
        return {}

    def save_channels(self):
        data = {str(guild_id): channel_id for guild_id, channel_id in self.channels.items()}
        text = json.dumps(data, ensure_ascii=False, indent=2)
        last_error = None
        for path in channel_config_candidates():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
                log.info('Saved channel config to %s', path)
                return path
            except OSError as error:
                last_error = error
                log.warning('Could not save channel config to %s: %s', path, error)
        raise last_error or OSError('Could not save channel config')

    def is_auto_music_channel(self, channel):
        return isinstance(channel, discord.TextChannel) and channel.name.lower().endswith(CHANNEL_NAME_SUFFIX)

    def should_handle_channel(self, channel):
        configured = self.channels.get(channel.guild.id)
        if configured is not None:
            return channel.id == configured
        if self.is_auto_music_channel(channel):
            return True
        return channel.id == DEFAULT_CHANNEL_ID

    def state_for(self, guild_id):
        state = self.states.get(guild_id)
        if state is None:
            state = GuildState()
            state.worker = asyncio.create_task(self.player(guild_id, state))
            self.states[guild_id] = state
        return state

    async def say(self, channel, text):
        try:
            await channel.send(text[:1900])
        except discord.HTTPException:
            log.warning('Could not send status message')

    async def on_ready(self):
        log.info('Connected as %s; default_channel=%s; channel_suffix=%s; configured_guilds=%s', self.user, DEFAULT_CHANNEL_ID, CHANNEL_NAME_SUFFIX, len(self.channels))

    def clear_queue(self, state):
        while not state.queue.empty():
            state.queue.get_nowait()
            state.queue.task_done()

    def enqueue_playlist_batch(self, state, channel):
        if not state.playlist:
            return 0, 0
        free_slots = state.queue.maxsize - state.queue.qsize()
        if free_slots <= 0:
            return 0, len(state.playlist.items) - state.playlist.offset
        amount = min(PLAYLIST_BATCH_SIZE, free_slots, len(state.playlist.items) - state.playlist.offset)
        start = state.playlist.offset
        for item_query, title in state.playlist.items[start:start + amount]:
            state.queue.put_nowait((channel, item_query, title))
        state.playlist.offset += amount
        left = len(state.playlist.items) - state.playlist.offset
        return amount, left

    async def configure_channel(self, message, parts):
        perms = message.author.guild_permissions
        if not (perms.manage_guild or perms.administrator):
            await self.say(message.channel, 'Настраивать канал может только участник с Manage Server или Administrator.')
            return
        if len(parts) != 2 or not parts[1].isdigit():
            await self.say(message.channel, 'Формат: lool channel_id')
            return
        channel_id = int(parts[1])
        channel = message.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await self.say(message.channel, 'Не вижу текстовый канал с таким ID на этом сервере.')
            return
        self.channels[message.guild.id] = channel_id
        try:
            saved_to = self.save_channels()
        except OSError:
            await self.say(message.channel, f'Канал применён до перезапуска, но файл настройки сохранить не получилось. Можно не использовать lool: просто назови музыкальный канал с окончанием {CHANNEL_NAME_SUFFIX}.')
            return
        await self.say(message.channel, f'Готово. На этом сервере слушаю только <#{channel_id}>. Настройка сохранена в {saved_to}.')

    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        text = message.content.strip()
        if not text:
            return

        parts = text.split()
        if parts and parts[0].lower() == 'lool':
            await self.configure_channel(message, parts)
            return

        if not self.should_handle_channel(message.channel):
            return

        state = self.state_for(message.guild.id)
        action, query = parse(text)
        playlist = None
        if action == 'play' and query and is_url(query):
            try:
                playlist = await asyncio.to_thread(extract_playlist, query)
            except Exception as error:
                log.warning('Playlist extraction failed in guild %s: %s', message.guild.id, type(error).__name__)

        async with state.lock:
            member_voice = getattr(message.author, 'voice', None)
            target = member_voice.channel if member_voice else None
            if not isinstance(target, discord.VoiceChannel):
                await self.say(message.channel, 'Сначала зайди в обычный голосовой канал.')
                return
            vc = message.guild.voice_client
            if vc and vc.channel != target:
                await self.say(message.channel, 'Зайди в мой голосовой канал, чтобы заказать трек или управлять музыкой.')
                return
            state.voice = vc
            if action == 'queue':
                upcoming = [item[2] for item in list(state.queue._queue)]
                tail = ''
                if state.playlist:
                    left = len(state.playlist.items) - state.playlist.offset
                    tail = f'\nПлейлист: {state.playlist.title}, осталось не загружено: {left}'
                await self.say(message.channel, 'Сейчас: ' + (state.current or 'тишина') + '\nОчередь:\n' + ('\n'.join(upcoming[:15]) or 'пуста') + tail)
                return
            if action == 'stop':
                state.generation += 1
                self.clear_queue(state)
                state.current = None
                state.playlist = None
                if vc:
                    vc.stop()
                    await vc.disconnect()
                state.voice = None
                await self.say(message.channel, 'Остановлено, очередь очищена.')
                return
            if action == 'skip':
                state.generation += 1
                if vc:
                    vc.stop()
                await self.say(message.channel, 'Перехожу к следующему треку.')
                return
            if action == 'playlist_next':
                added, left = self.enqueue_playlist_batch(state, message.channel)
                if added:
                    await self.say(message.channel, f'Докинул из плейлиста: {added}. Осталось не загружено: {left}.')
                elif state.playlist:
                    await self.say(message.channel, 'Очередь заполнена или плейлист уже закончился.')
                else:
                    await self.say(message.channel, 'Сначала скинь ссылку на плейлист.')
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
                await self.say(message.channel, 'Напиши название или ссылку длиной до 500 символов.')
                return
            if state.queue.full():
                await self.say(message.channel, f'Очередь заполнена ({QUEUE_MAX_SIZE} запросов).')
                return
            if not vc or not vc.is_connected():
                try:
                    state.voice = await target.connect(timeout=30, self_deaf=True)
                except (discord.DiscordException, asyncio.TimeoutError):
                    await self.say(message.channel, 'Не удалось подключиться. Проверь мои права Connect и Speak.')
                    return
            if playlist:
                state.playlist = PlaylistSession(playlist['title'], playlist['items'])
                added, left = self.enqueue_playlist_batch(state, message.channel)
                await self.say(message.channel, f'Плейлист найден: {discord.utils.escape_markdown(state.playlist.title)}. Добавил {added} треков, осталось не загружено: {left}. Напиши «дальше», чтобы докинуть следующую порцию.')
                return
            state.queue.put_nowait((message.channel, query, query))
            await self.say(message.channel, 'Добавлено: ' + discord.utils.escape_markdown(query))

    async def player(self, guild_id, state):
        while True:
            channel, query, label = await state.queue.get()
            version = state.generation
            vc = state.voice
            state.current = label
            source = None
            try:
                info = await asyncio.to_thread(extract, query)
                async with state.lock:
                    if version != state.generation or vc is not state.voice or not vc or not vc.is_connected():
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
                    source = None
                    state.current = info.get('title', label or query)
                await self.say(channel, 'Играет: ' + discord.utils.escape_markdown(state.current or query))
                error = await done
                if error:
                    await self.say(channel, 'Поток прервался. Перехожу к следующему запросу.')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Playback failed in guild %s: %s', guild_id, type(error).__name__)
                if version == state.generation:
                    await self.say(channel, 'Не удалось воспроизвести запрос. Попробуй другое название или ссылку; если повторяется — проверь журнал сервера и обнови yt-dlp.')
            finally:
                if source:
                    source.cleanup()
                state.current = None
                state.queue.task_done()

    async def on_voice_state_update(self, member, before, after):
        if member.id != self.user.id or not before.channel or after.channel is not None:
            return
        state = self.states.get(before.channel.guild.id)
        if not state:
            return
        state.generation += 1
        self.clear_queue(state)
        if state.voice:
            state.voice.stop()
        state.voice = None

    async def close(self):
        for state in self.states.values():
            if state.worker:
                state.worker.cancel()
        for vc in self.voice_clients:
            await vc.disconnect(force=True)
        await asyncio.gather(*(state.worker for state in self.states.values() if state.worker), return_exceptions=True)
        await super().close()


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in environment or .env')
    for binary in ('ffmpeg', 'deno'):
        if not shutil.which(binary):
            raise SystemExit(f'Install {binary} and add it to PATH')
    MusicBot().run(token)
