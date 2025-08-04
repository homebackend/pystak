""" Download, Search books from various sources """

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from abc import ABC, abstractmethod
from typing import Dict, Any


import aiofiles
import aiohttp
import libtorrent
from lxml import html
from tqdm import tqdm
from aiohttp_xmlrpc import handler
from aiohttp_xmlrpc.client import ServerProxy
from internetarchive.session import ArchiveSession
from internetarchive import get_item, get_files, search
from pysmartdl2 import SmartDL, HashFailedException
from telethon.sync import TelegramClient
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

DEFAULT_TIMEOUT = 30
DEFAULT_PEER_WAIT_TIME = 180
DEFAULT_PARALLELISM = 5
DEFAULT_PORT = 6881
MAX_SEARCH_COUNT = 12
DEFAULT_TORRENT_CACHE = './torrent-cache.json'
DEFAULT_ARCHIVE_CACHE = './archive-cache.json'
DEFAULT_IND_CULT_CACHE = './indian-culture-cache.json'
DEFAULT_TELEGRAM_CACHE = './telegram-cache.json'
DEFAULT_INT_CULT_METADATA = 'metadata.json'
DEFAULT_TELEGRAM_SESSION = './telegram.session'
ARCHIVE_ORG_API_LIMIT = 25
ROTATING_TEXT_SIZE = 40
SOCK_FILE = f'/tmp/{os.path.basename(sys.argv[0]).replace(".py", "")}.sock'
BOOK_XPATHS = {
    "collection": "/html/body/main/div[3]/ul/li[last()]/div/a[1]",
    "torrent_exists": "/html/body/main/div[3]/ul/li[last()]/text()",
    "torrent": "/html/body/main/div[3]/ul/li[last()]/div/a[2]/text()",
    "torrent_url": "/html/body/main/div[3]/ul/li[last()]/div/a[2]/@href",
    "filename_within_torrent": "/html/body/main/div[3]/ul/li[last()]/div/text()[3]",
    "title": "/html/body/main/div[1]/div[3]/text()",
    "extension": "/html/body/main/div[1]/div[2]/text()"
}
ANNAS_SEARCH_XPATH = {
    'search_results': "/html/body//form/div/div/div/div[@id='aarecord-list']/div/a",
    'next_pages': "//div[@id='aarecord-list']/div[contains(@class, 'js-scroll-hidden')]",
}


replace_chars = str.maketrans(dict.fromkeys(
    ''.join([" /"]), '.') | dict.fromkeys(''.join([":;"]), None))


class RateLimiter:
    """Used to rate limit any task"""

    def __init__(self, max_tasks_per_unit_time, unit_time=60):
        self.max_tasks_per_unit_time = max_tasks_per_unit_time
        self.tasks_launched_in_unit_time = 0
        self.unit_time = unit_time
        self.start_time = time.monotonic()
        self.tasks_launched = 0
        self.handle_lock = True
        self.lock = asyncio.Lock()

    async def acquire(self):
        """"Call aquire to limit the task you are executing"""

        current_time = time.monotonic()
        elapsed_time = current_time - self.start_time

        if elapsed_time >= self.unit_time:
            self.tasks_launched_in_unit_time = 0
            self.start_time = current_time

        if self.tasks_launched_in_unit_time >= self.max_tasks_per_unit_time:
            wait_time = self.unit_time - elapsed_time
            if not self.handle_lock:
                print(
                    '::: API Rate limit hit. Waiting for about 1 minute before continuing')
            self.handle_lock = True
            await asyncio.sleep(wait_time)
            async with self.lock:
                if self.handle_lock:
                    self.handle_lock = False
                    self.tasks_launched_in_unit_time = 0
                    self.start_time = time.monotonic()

        self.tasks_launched_in_unit_time += 1
        self.tasks_launched += 1
        # print(f'Allowing task {self.tasks_launched}')


class TqdmDownloadHandler(tqdm):
    """Handles downloading via tqdm"""

    def __init__(self,
                 **kwargs):
        super().__init__(**kwargs)

        self.desc_start = 0
        self.desc_forward = True

    def set_rotating_description_str(self, full_desc=None, refresh=True, prefix: str = None, icon: str = None):
        """Set/modify description without ': ' appended."""

        if len(full_desc) <= ROTATING_TEXT_SIZE:
            desc = full_desc
            self.desc_start = 0
            self.desc_forward = True
        else:
            desc = full_desc[self.desc_start:self.desc_start +
                             ROTATING_TEXT_SIZE]
            if self.desc_forward:
                if self.desc_start + ROTATING_TEXT_SIZE >= len(full_desc):
                    self.desc_forward = False
                    # because length itself can change
                    self.desc_start = len(full_desc) - ROTATING_TEXT_SIZE
                    # across runs to ensure that we don't overrun
                else:
                    self.desc_start += 1
            else:
                if self.desc_start <= 0:
                    self.desc_forward = True
                    self.desc_start = 0
                else:
                    self.desc_start -= 1

        prefix = f"{prefix:10.10s}" if prefix is not None else ''
        desc = f"{prefix}{icon + ' ' if icon is not None else ''}{desc}"

        self.set_description(desc, refresh)


class Downloader(ABC):
    """Downloader base class to manage downloads"""

    ICON_WAITING_FOR_DOWNLOAD = '🤨'
    ICON_DOWNLOADING = '😊'

    def __init__(self, args, config):
        my_config = {
            'timeout': args.timeout,
            'parallelism': args.parallelism,
            'temp_file_path': args.temp_file_path,
            'save_path': args.save_path,
            'skip_cache': args.skip_cache,
            'download_all': args.download_all,
        }
        self.config = {**my_config, **config}
        self.lock = asyncio.Lock()
        self.queue = None
        self.pending_urls = set()
        self.url_errors = {}
        self.final_results = {}
        self.stats = {
            'torrent_completed': 0,
            'torrent_count': 0,
            'data_completed': 0,
            'data_size': 0,
        }

    async def exec_tasks(self, task, items, parallelism, position_offset=0):
        """Exectures the given tasks in event loop"""

        q = asyncio.Queue()
        self.queue = q

        loop = asyncio.get_event_loop()
        workers = [loop.create_task(task(i + position_offset, q))
                   for i in range(parallelism)]

        for item in items:
            await q.put(item)

        await q.join()

        self.queue = None
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def monitor_downloaded_data_task(self, position: int):
        """
        Monitor overall downloaded data progress. This will monitor the amount of data
        that has been downloaded for all the files.
        """

        with tqdm(position=position, unit='B', unit_scale=True, leave=False,
                  unit_divisor=1024, miniters=1,
                  total=self.stats['data_size'] if self.stats['data_size'] > 0 else 1024*1024*1024,
                  desc='Downloaded data'
                  ) as pbar:
            old = 0
            while True:
                if pbar.total != self.stats['data_size']:
                    pbar.total = self.stats['data_size']

                if old != self.stats['data_completed']:
                    pbar.update(self.stats['data_completed'] - old)
                    old = self.stats['data_completed']
                pbar.refresh()

                await asyncio.sleep(0.2)

    async def monitor_download_task(self, position: int):
        """
        Monitors the count of files that have been downloaded. Depending on source
        one source can map to one or more files.
        """

        total = len(self.final_results)
        count = 0

        with tqdm(
                position=position, unit='file', miniters=1,
                desc='Downloaded files',
                total=total,
                leave=False
        ) as pbar:
            count = 0
            while count < total:
                if total != len(self.final_results):
                    total = len(self.final_results)
                    pbar.total = total
                    pbar.refresh()

                old = count
                count = 0
                for _, r in self.final_results.items():
                    count += 1 if r['downloaded'] else 0

                if old != count:
                    pbar.update(count - old)
                pbar.refresh()

                await asyncio.sleep(0.2)

    async def process_downloading_init(self, items):
        """Initialises processing of downloading by creating other tqdm and other processing if required"""
        return 1

    async def get_file_hash(self, file_name, hash_algorithm="sha256"):
        """Calculate hash of a given file"""

        hasher = hashlib.new(hash_algorithm)
        async with aiofiles.open(file_name, 'rb') as f:
            while True:
                chunk = await f.read(4096)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    async def is_file_already_downloaded(self, file_name):
        """
        Checks if file is already downloded. Assumes that if both {file-name}
        and {file-name}-hash exist and content of {file-name}-hash is 
        sha256 hash of {file-name} then {file-name} is already downloaded
        """

        hash_file_name = f'{file_name}-hash'
        if not os.path.exists(file_name) or not os.path.exists(hash_file_name):
            return False

        file_hash_read = ''
        with open(hash_file_name, 'r', encoding='utf-8') as f:
            file_hash_read = f.read().strip()

        file_hash = await self.get_file_hash(file_name)

        return file_hash == file_hash_read

    async def save_file_hash(self, file_name):
        """Save file hash"""

        hash_file_name = f'{file_name}-hash'
        if not os.path.exists(file_name) or os.path.exists(hash_file_name):
            return

        with open(hash_file_name, 'w', encoding='utf-8') as f:
            file_hash = await self.get_file_hash(file_name)
            f.write(file_hash)

    async def complete_single_file_download(self, uniq_id, size):
        """
        Marks comple single file download - i.e. urls which contain single files.
        This excludes, for example, torrents.
        """

        async with self.lock:
            self.stats['data_completed'] += size
            self.final_results[uniq_id]['downloaded'] = True
            self.pending_urls.remove(uniq_id)

    @abstractmethod
    async def process_download_worker(self, position: int, q: asyncio.Queue, options: Dict[str, Any]):
        """
        Processes a given download item. Each different type of source will have their
        own implementation.
        """

    async def process_downloading(self, items, options: Dict[str, Any]):
        """Process all downloads to completion. This will execute download in parallel based on configured value"""

        loop = asyncio.get_event_loop()

        offset = await self.process_downloading_init(items)
        loop.create_task(self.monitor_downloaded_data_task(offset))
        offset += 1
        loop.create_task(self.monitor_download_task(offset))
        offset += 1

        await self.exec_tasks(
            lambda p, q: self.process_download_worker(p, q, options),
            items,
            options['parallelism'],
            position_offset=offset)

    @abstractmethod
    async def download(self, urls):
        """Starts download for the given list of urls"""

    async def download_file(self,
                            position: int,
                            file_path: str,
                            url: str,
                            display_text: str,
                            size: int,
                            sha1_hash: str = None,
                            headers=None):
        """Helper function to download a URL."""

        if sha1_hash is None:
            # for files for which sha1_hash is not present we check
            # if file {file_path}-hash exists and does it have the
            # correct hash
            if not await self.is_file_already_downloaded(file_path):
                return True, size

        count = 0

        request_args = None if headers is None else {'headers': headers}

        while True:
            try:
                handle = SmartDL(
                    [url], file_path, progress_bar=False, request_args=request_args)
                break
            except urllib.error.URLError as e:
                self.url_errors[url] = str(e)
                return False, size
            except TimeoutError:
                if count < 3:
                    count += 1
                    await asyncio.sleep(0.2*count)
                else:
                    self.url_errors[url] = 'timeout happened while downloading'
                    return False, size

        if sha1_hash is not None:
            handle.add_hash_verification('sha1', sha1_hash)

        current = 0

        with TqdmDownloadHandler(
            position=position, unit='B', unit_scale=True,
            unit_divisor=1024, miniters=1, desc=display_text,
            total=size if size > 0 else 1024*1024*1024, leave=False,
        ) as pbar:
            count = 0
            while True:
                try:
                    handle.start(blocking=False)

                    while not handle.isFinished():
                        if size == 0:
                            # This is the case when size of file is not known beforehand
                            # so we update once we come to know the actual size
                            size = handle.get_final_filesize()
                            if size > 0:
                                pbar.total = size
                                # Update the size for other progress bar as well
                                self.stats['data_size'] += size

                        downloaded = handle.get_dl_size()
                        if downloaded > current:
                            pbar.update(downloaded - current)
                            current = downloaded
                        pbar.set_rotating_description_str(display_text)
                        await asyncio.sleep(0.2)

                    if handle.isSuccessful():
                        pbar.update(size - current)

                except HashFailedException as e:
                    self.url_errors[url] = f'hash verification failed: {e}'
                    return False, size
                except (urllib.error.HTTPError, urllib.error.URLError) as e:
                    self.url_errors[url] = f'error downloading: {e}'
                    return False, size
                except RuntimeError as e:
                    if str(e) == 'cannot start (current status is finished)':
                        break
                    self.url_errors[url] = f'error during download: {e}'
                    return False, size
                except TimeoutError:
                    count += 1
                    if count > 3:
                        return False, size
                    print(f':: Timeout occured ... will retry {count}')
                    asyncio.sleep(count*1.0)

        if sha1_hash is None:
            await self.save_file_hash(file_path)

        return True, size

    @abstractmethod
    async def search(self, text):
        """
        Searches for user text and downloads preferred matching items.
        """

    async def search_selection_page(self, results, end, start=1, start_count=0, total_count=None):
        """Allows user to select from amongst the search results"""

        selections = set()
        message = f':: Showing search results {start} - {end}'
        if total_count is not None:
            message += f' from a total of {total_count}'
        print(message)
        has_desc = len(results[0]) > 2
        for i in range(start - 1, end):
            if has_desc:
                print(f'{i + 1 - start_count:4d} {results[i][1]:100.100s}')
                print(f'        {results[i][2]:100.100s}')
            else:
                print(f'{i + 1 - start_count}\t{results[i][1]:100.100s}')
        print(':: Results to download: (e.g.: 3,4,6-8,14-17))')
        print('::                      (a: select all items from this page)')
        print(
            '::                      (e: select everything from this and rest of the pages)')
        print('::                      (s: skip this and rest of the pages)')
        user_input = input(
            '==> Your input: ')
        if user_input.strip().lower() in ['q', 's', 'e']:
            return user_input.strip().lower()

        if user_input.strip().lower() == 'a':
            for i in range(start - 1, end):
                selections.add(results[i][0])
            return selections

        items = self.parse_selection_string(user_input)
        for i in items:
            selections.add(results[i - 1 + start_count][0])

        return selections

    async def prompt_for_download(self, selections):
        """Prompt for continuing with download"""

        while True:
            user_input = input(
                f"Do you want to proceed with download of {len(selections)} items? (yes/no): ").lower()
            if user_input == 'yes' or user_input == 'y':
                await self.download(selections)
                break
            elif user_input == 'no' or user_input == 'n':
                print('Operation cancelled.')
                break
            else:
                print("Invalid input. Please enter 'yes' or 'no'.")

    def parse_selection_string(self, selection_str):
        """
        Parses for numbers, ranges, and validates input
        """

        selected_numbers = set()  # Use a set to automatically handle duplicates

        parts = selection_str.split(',')
        for part in parts:
            part = part.strip()  # Remove leading/trailing whitespace

            if not part:  # Handle empty entries if there are consecutive commas or leading/trailing commas
                continue

            if '-' in part:  # It's a range
                try:
                    start_str, end_str = part.split('-')
                    start = int(start_str)
                    end = int(end_str)
                    # Ensure the range is valid (start <= end)
                    for num in range(start, end + 1):
                        selected_numbers.add(num)
                except ValueError:
                    print(f"Warning: Invalid range format '{part}'. Skipping.")
            else:  # It's a single number
                try:
                    num = int(part)
                    selected_numbers.add(num)
                except ValueError:
                    print(
                        f"Warning: Invalid number format '{part}'. Skipping.")

        # Convert to list and sort for consistent output
        return sorted(list(selected_numbers))

    async def process_search_results(self, results):
        """
        Process search results for user interaction followed
        by download.
        """

        if self.config['download_all']:
            print(':: Downloading all files as per user request')
            selections = set()
            for r in results:
                selections.add(r[0])
            await self.download(selections)
            return

        selections = set()
        total_count = len(results)
        start_count = 0

        while start_count < total_count:
            start = start_count + 1
            end = start_count + MAX_SEARCH_COUNT if start_count + \
                MAX_SEARCH_COUNT < total_count else total_count

            print()
            current = await self.search_selection_page(
                results, end, start, start_count, total_count)
            if isinstance(current, str):
                if current == 's':
                    break
                elif current == 'q':
                    print(':: Quiting on user request.')
                    # User quit
                    return
                elif current == 'e':
                    for i in range(start - 1, total_count):
                        selections.add(results[i][0])
                    break
            selections.update(current)

            start_count += MAX_SEARCH_COUNT

        if not selections:
            print('No user selections were made. Exiting.')
            return

        await self.prompt_for_download(selections)

    def clear_screen(self):
        """Clears the terminal screen based on the operating system."""
        # For Windows
        if os.name == 'nt':
            _ = os.system('cls')
        # For macOS and Linux (posix systems)
        else:
            _ = os.system('clear')

    def show_download_results(self):
        """Print download results"""

        if self.final_results or self.pending_urls:
            self.clear_screen()

        if self.final_results:
            print("\n\nDownload status:")
            found = False
            for _, result in self.final_results.items():
                if result['downloaded']:
                    found = True
                    print(f"✅ {result['file']}")
                # else:
                    # print(f"❌ {result['file']}")
            if not found:
                print('❌ No downloads completed')

        if self.pending_urls:
            print("\nThe following urls are still pending download:")
            for u in self.pending_urls:
                if u in self.url_errors:
                    print(f' - {u} ({self.url_errors[u]})')
                else:
                    print(f' - {u}')

        print()

    def save_download_results(self):
        """Save current download results"""
        with open(f'run-end-{time.time()}.txt', 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'run': self.NAME,
                'config': self.config,
                'final_results': self.final_results,
                'stats': self.stats,
                'pending_urls': list(self.pending_urls),
                'url_errors': self.url_errors,
            }, indent=4))


class DownloaderWithDetails(Downloader):
    """
    This downloader class is to be used when separate step
    is needed for getting for getting details about download.
    """

    @abstractmethod
    async def get_details_worker(self, position: int, q: asyncio.Queue, results, options: Dict[str, Any]):
        """Get details required to download the given item"""

    @abstractmethod
    def get_cache_file(self):
        """
        Get cache file for downloader. This file will be read as JSON to
        read cache. If None is returned no cache will be used.
        Each implementation should ideally have their own cache file.
        """

    @abstractmethod
    async def get_file_index(self):
        """Get index of file value in result tuple"""

    @abstractmethod
    async def get_uniq_value(self, key: str):
        """Get string that will make filename unique in case of clash"""

    @abstractmethod
    async def get_result_uniq_key(self, key: str, result):
        """Get string that is used in final_result"""

    async def get_dup_count(self, results):
        """Gets count against each file name"""

        files_count = {}
        index = await self.get_file_index()

        for _, result in results.items():
            files_count[result[index]] = files_count.get(result[index], 0) + 1

        return files_count

    async def handle_dups(self, results):
        """Handles where items have same file name"""
        files_count = await self.get_dup_count(results)

        for k, result in results.items():
            index = await self.get_file_index()
            org_file = result[index]
            if files_count[org_file] > 1:
                new_file = await self.get_uniq_value(k) + '-' + org_file
                new_result = list(result)
                new_result[index] = new_file
                results[k] = tuple(new_result)
                rkey = await self.get_result_uniq_key(k, result)
                rkey = f'{rkey}-{org_file}'
                # print(rkey)
                if rkey in self.final_results:
                    del self.final_results[f'{rkey}-{org_file}']

        # print(json.dumps(self.final_results, indent=4))

    async def get_details(self, urls, options: Dict[str, Any]):
        """
        Get details required to download the given items.
        This could involve one or more steps depending on source.
        It also supports reading from cache to avoid refetching details.
        """

        print(f'Fetch details for {len(urls)} items ', end='')

        results = {}

        pending_urls = []
        cache_file = self.get_cache_file()

        if not self.config['skip_cache'] and cache_file is not None and os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding="utf-8") as file:
                    all_results = json.load(file)
                    for u, v in all_results.items():
                        if u in urls:
                            results[u] = v

            except FileNotFoundError:
                print(f"Error: {cache_file} not found.")
            except json.JSONDecodeError:
                print(
                    f"Error: Invalid JSON format in {cache_file}.")

        for u in urls:
            if u not in results:
                pending_urls.append(u)

        if not pending_urls:
            await self.handle_dups(results)
            print(flush=True)
            return results

        await self.exec_tasks(lambda p, q: self.get_details_worker(p, q, results, options),
                              pending_urls,
                              self.config['parallelism'])

        await self.handle_dups(results)

        if cache_file is not None:
            if os.path.exists(cache_file):
                with open(cache_file, 'r', encoding="utf-8") as f:
                    all_results = json.load(f)
            else:
                all_results = {}
            for u, v in results.items():
                all_results[u] = v
            with open(cache_file, 'w', encoding="utf-8") as f:
                json.dump(all_results, f, indent=4)

        with open(f'run-start-{time.time()}.txt', 'w', encoding='utf-8') as f:
            for u, _ in results.items():
                f.write(f'{u}\n')

        # print(f'Fetch details for {len(urls)} items ...')
        print(flush=True)

        return results


class XMLRPCDownloader(handler.XMLRPCView):
    """Class to act as RPC Handler"""

    async def rpc_ping(self):
        """Just ping pong"""
        return 'pong'

    async def rpc_process_torrent(self, path,
                                  torrent,
                                  url,
                                  files,
                                  save_path,
                                  assume_downloaded):
        """Add torrent and enqueue"""
        downloader = self.request.app['downloader']
        return await downloader.process_torrent(
            path, torrent, url, files,
            save_path, assume_downloaded)


class HttpDownloader(DownloaderWithDetails):
    """
    Base class for downloading from http urls
    """

    @abstractmethod
    def get_download_info(self, save_path, data):
        """Get information about download"""

    async def process_download_worker(self, position: int, q: asyncio.Queue, options: Dict[str, Any]):
        """
        Processes a given download item. Each different type of source will have their
        own implementation.
        """
        save_path = self.config['save_path']

        while True:
            data = await q.get()
            uniq_id, path, url, title, size, sha1, headers = self.get_download_info(
                save_path, data)
            if url is not None:
                success, size = await self.download_file(position, path, url,
                                                         f'Downloading {title}',
                                                         size, sha1, headers)
                if success:
                    await self.complete_single_file_download(uniq_id, size)

            q.task_done()
            # Voluntary sleep so that other tasks such as updating of tqdm can occur
            await asyncio.sleep(0.2)


class SourceHtmlPageDownloader(DownloaderWithDetails):
    """
    Downloader for fetching URLs from HTML pages.
    """

    @abstractmethod
    async def process_content(self, response: aiohttp.ClientResponse):
        """
        Process content from html page.
        Each source will have separate implementation.
        """

    async def get_details_worker(self, position: int, q: asyncio.Queue, results, options: Dict[str, Any]):
        """Get details required to download the given item from Html Page."""

        session = options['session']

        while True:
            url = await q.get()

            if url not in results:
                print('.', end='', flush=True)
                async with session.get(url) as response:
                    try:
                        results[url] = await self.process_content(response)
                    except (IndexError, ValueError) as e:
                        self.url_errors[url] = str(e)

            q.task_done()

    async def get_details(self, urls, options: Dict[str, Any]):
        cto = aiohttp.ClientTimeout(total=self.config['timeout'])

        async with aiohttp.ClientSession(timeout=cto) as session:
            options['session'] = session
            return await super().get_details(urls, options)

    @abstractmethod
    def get_search_item(self, o):
        """Search item from html tree"""

    @abstractmethod
    async def get_search_items(self, session, url):
        """Parses page and get results"""

    @abstractmethod
    def get_search_url(self):
        """Get url to use for searching"""

    @abstractmethod
    def get_list_page_url(self, base_url: str, page: int):
        """ Get url of list page given base url"""

    async def search_worker(self, p, q: asyncio.Queue, session, results):
        """Runs search request in background"""

        while True:
            url = await q.get()
            current, _ = await self.get_search_items(session, url)
            results.extend(current)
            q.task_done()

    async def get_listing_items(self, url: str):
        """"Get listing item from page and navigated pages"""

        cto = aiohttp.ClientTimeout(total=self.config['timeout'])
        async with aiohttp.ClientSession(timeout=cto) as session:
            results, page_count = await self.get_search_items(session, url)
            if page_count > 1:
                print(
                    f':: Searching additional {page_count - 1} page(s) ... please wait ...')
                urls = [self.get_list_page_url(url, i)
                        for i in range(2, page_count + 1)]
                await self.exec_tasks(
                    lambda p, q: self.search_worker(p, q, session, results),
                    urls, self.config['parallelism'])

            return results

    async def search(self, text):
        """
        Searches for user text and downloads preferred matching items.
        """

        search_str = ' '.join(text)
        print(f':: Searching "{search_str}"')
        url = f'{self.get_search_url()}{urllib.parse.quote_plus(search_str)}'
        results = await self.get_listing_items(url)
        await self.process_search_results(results)


class TorrentDownloader(SourceHtmlPageDownloader):
    """Manages downloads from torrent"""

    ICON_WAITING_FOR_PEERS = '😴'
    lt_session = None

    def __init__(self, args, config):
        my_config = {
            'port': args.port,
        }
        super().__init__(args, {**my_config, **config})
        self.init_libtorrent()
        self.torrent_url_mapping = {}
        self.torrent_file_url_mapping = {}
        self.sentinel = set()

    async def start_rpc(self):
        """Start RPC server"""

        app = aiohttp.web.Application()
        app['downloader'] = self
        app.router.add_route('*', '/rpc', XMLRPCDownloader)

        socket_path = SOCK_FILE
        if os.path.exists(socket_path):
            os.remove(socket_path)

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.UnixSite(runner, socket_path)
        await site.start()

        # print(f":: XML-RPC server listening on Unix socket: {socket_path}")

        # Keep the server running until interrupted
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep for a long time
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()

    async def file_search(self, torrent_info, files, torrent, save_path,
                          assume_downloaded):
        """
        Search for files to be downloaded in torrent info object
        """

        priorities = []
        renames = {}

        file_data = []
        found_files = set()

        for idx, des in enumerate(torrent_info.orig_files()):
            found = False
            for file in files:
                if des.path.endswith(file[0]):
                    found = True
                    found_files.add(file[0])

                    if assume_downloaded and os.path.exists(f'{save_path}/{file[1]}'):
                        priorities.append(0)
                        self.final_results[f"{torrent}-{file[1]}"]['downloaded'] = True

                        async with self.lock:
                            self.stats['data_size'] += des.size
                            self.stats['data_completed'] += des.size
                    else:
                        priorities.append(255)
                        renames[idx] = file[1]
                        file_data.append({
                            'save_name': file[1],
                            'idx': idx,
                            'size': des.size,
                            'path': des.path,
                        })

                        async with self.lock:
                            self.stats['data_size'] += des.size
                    break
            if not found:
                priorities.append(0)

        # Handle files that were not found in torrent
        for file in files:
            if file[0] not in found_files:
                url = self.torrent_file_url_mapping[f'{torrent}-{file[0]}']
                self.pending_urls.add(url)
                self.url_errors[url] = 'file not found in torrent'

        return (file_data, priorities, renames)

    async def add_torrent(self, results, torrent_path: str,
                          torrent: str, files, save_path: str,
                          assume_downloaded: bool):
        """Function adds torrent to libtorrent session in a stopped state"""

        info = libtorrent.torrent_info(torrent_path)

        # print(info)

        h = self.lt_session.find_torrent(info.info_hash())
        if h.is_valid():
            # Torrent found as hash already exists.
            print(f'Torrent already added: {torrent}')
            return False

        async with self.lock:
            self.stats['torrent_count'] += 1

        file_data, priorities, renames = await self.file_search(
            info, files, torrent, save_path, assume_downloaded)
        if not file_data:  # No file to download - all are already downloaded
            await self.complete_torrent(torrent)
            return False

        size = 0
        for f in file_data:
            size += f['size']

        atp = libtorrent.add_torrent_params()
        atp.ti = info
        atp.save_path = save_path
        atp.file_priorities = priorities
        atp.renamed_files = renames
        atp.auto_managed = False
        atp.flags |= libtorrent.torrent_flags.stop_when_ready
        atp.flags &= ~libtorrent.torrent_flags.auto_managed

        try:
            h = self.lt_session.add_torrent(atp)
            h.pause()

            results.append((h, torrent, files, size, file_data, 1))
            return True
        except Exception as e:
            print(f'Some error happened while adding torrent: {e}')
            return False

    async def download_torrent_worker(self,
                                      position: int,
                                      q: asyncio.Queue,
                                      results,
                                      session: aiohttp.ClientSession,
                                      options: Dict[str, Any]):
        """Downloads a torrent from given url and adds to libtorrent in stopped state"""

        temp_path = self.config['temp_file_path']
        save_path = self.config['save_path']
        assume_downloaded = self.config['assume_downloaded']

        while True:
            url, torrent, actual_url, files = await q.get()
            path = f'{temp_path}/{torrent}'
            # print(files)

            # await self.download_file(session, position, path, url, f"downloading {torrent}", noui=noui)

            desc = f"downloading {torrent}"

            if not os.path.exists(path):
                try:
                    with open(path, 'wb') as fout:
                        async with session.get(url, allow_redirects=True) as response:
                            with TqdmDownloadHandler(
                                position=position, unit='B', unit_scale=True,
                                unit_divisor=1024, miniters=1, desc=desc,
                                total=int(response.headers.get(
                                    'content-length', 0))
                            ) as pbar:
                                pbar.set_rotating_description_str(desc)
                                async for chunk in response.content.iter_chunked(4096):
                                    fout.write(chunk)
                                    pbar.update(len(chunk))
                                    pbar.set_rotating_description_str(desc)
                                pbar.update(
                                    int(response.headers.get('content-length', 0)))
                                pbar.set_description_str(
                                    f'downloaded {torrent}', refresh=True)
                except Exception as e:
                    print(
                        f"Error occurred while downloading torrent {torrent}: {e}")
                    self.url_errors[actual_url] = 'error downloading torrent'
                    q.task_done()
                    return

            if 'is_rpc' in options and options['is_rpc']:
                client = options['rpc_client']
                res = await client.process_torrent(path, torrent, actual_url, files, save_path, assume_downloaded)
                results.append(res)
            else:
                await self.add_torrent(results, path, torrent, files, save_path, assume_downloaded)
            q.task_done()

    async def download_torrent(self, torrents, options: Dict[str, Any]):
        """Download all torrents and add to libtorrent as stopped"""

        results = []

        for _, torrent, _, files in torrents:
            for f in files:
                self.final_results[f'{torrent}-{f[1]}'] = {
                    'file': f[1],
                    'downloaded': False
                }

        cto = aiohttp.ClientTimeout(total=self.config['timeout'])

        async with aiohttp.ClientSession(timeout=cto) as session:
            await self.exec_tasks(
                lambda p, q: self.download_torrent_worker(
                    p, q,
                    results,
                    session,
                    options),
                torrents,
                self.config['parallelism'])

        return results

    async def monitor_libtorrent_alerts(self, torrents):
        """Monitors libtorrent alert taking appropriate actions"""

        while True:
            alerts = self.lt_session.pop_alerts()

            for a in alerts:
                alert_type = type(a).__name__
                if alert_type == 'file_completed_alert':
                    for _, torrent, _, _, file_data, _ in torrents:
                        for f in file_data:
                            if a.index == f['idx']:
                                self.final_results[f"{torrent}-{f['save_name']}"]['downloaded'] = True

                                async with self.lock:
                                    self.stats['data_completed'] += f['size']
                                break

            await asyncio.sleep(0.2)

    async def monitor_torrents_task(self, position: int):
        """Monitor torrent progress"""

        with tqdm(
                position=position, unit='torrent', miniters=1,
                desc='Downloaded torrents',
                total=self.stats['torrent_count'],
                leave=False
        ) as pbar:
            old = 0
            while self.stats['torrent_completed'] < self.stats['torrent_count']:
                if self.stats['torrent_count'] != pbar.total:
                    pbar.total = self.stats['torrent_count']

                if old != self.stats['torrent_completed']:
                    pbar.update(self.stats['torrent_completed'] - old)
                    old = self.stats['torrent_completed']
                pbar.refresh()

                await asyncio.sleep(0.2)

    def files_downloaded(self, torrent, file_data):
        """Checks if all files have been downloaded for a particular torrent"""

        for f in file_data:
            if not self.final_results[f'{torrent}-{f["save_name"]}']['downloaded']:
                return False

        return True

    def add_torrent_url_mapping(self, torrent, url):
        """Add torrent to url mapping"""
        self.torrent_url_mapping.setdefault(torrent, []).append(url)

    async def complete_torrent(self, torrent, file_data=None):
        """Performs actions that need to be done after torrent completion"""

        async with self.lock:
            self.stats['torrent_completed'] += 1
            for u in self.torrent_url_mapping[torrent]:
                if u in self.pending_urls and u not in self.url_errors:
                    # If there was error for url that means this torrent download
                    # didn't complete download of url. The most common case is if
                    # file was not present in torrent. Since a torrent can have
                    # multiple files. Torrent completion may not mean that file
                    # was downloaded successfully.
                    self.pending_urls.remove(u)

            if file_data is not None:
                for f in file_data:
                    self.final_results[f"{torrent}-{f['save_name']}"]['downloaded'] = True

    async def process_downloading_init(self, items):
        loop = asyncio.get_event_loop()

        loop.create_task(self.monitor_libtorrent_alerts(items))
        offset = 1
        loop.create_task(self.monitor_torrents_task(offset))
        offset += 1

        return offset

    async def process_download_worker(self, position: int, q: asyncio.Queue, options: Dict[str, Any]):
        """Process a torrents to completion"""

        count = 1

        while True:
            if q.empty():
                self.sentinel.add(position)
                if len(self.sentinel) >= self.config['parallelism']:
                    # So all loops have no work, so exit.
                    break

                # Other loops are executing. So let's continue waiting
                # additional work may come.
                await asyncio.sleep(0.2)
                continue

            h, torrent, files, size, file_data, tries = await q.get()

            s = h.status()
            h.resume()

            start = time.time()
            non_zero_peer_count = 0
            last_download = start
            torrent_last_download = 0

            file_txt = ''
            if len(file_data) > 2:
                file_txt = f'{file_data[0]["save_name"]} + {len(file_data) - 1} more files'
            elif len(file_data) > 1:
                file_txt = f'{file_data[0]["save_name"]} + one more file'
            else:
                file_txt = f'{file_data[0]["save_name"]}'

            base_desc = f"   {{}} #{tries} {{}} peer(s){{}}{{}} {{:.4f}} {file_txt}.   "
            prefix = f"#{position - 3}.{count} <{len(file_data)}>"

            old = 0
            with TqdmDownloadHandler(position=position, unit='B', unit_scale=True,
                                     leave=False, unit_divisor=1024, miniters=1,
                                     total=size) as pbar:
                while True:
                    icon = Downloader.ICON_DOWNLOADING
                    s = h.status()
                    end = time.time()

                    if s.state == s.finished or self.files_downloaded(torrent, file_data):
                        await self.complete_torrent(torrent)
                        pbar.update(size)
                        pbar.set_description_str("finished", refresh=True)
                        self.lt_session.remove_torrent(h)
                        q.task_done()
                        break
                    if (s.num_peers == 0 and end - start >= self.config['peer_wait_time']) or \
                            end - last_download >= 3 * self.config['peer_wait_time']:
                        # We have waited enough for peers
                        h.pause()
                        await q.put((h, torrent, files, size, file_data, tries + 1,))
                        q.task_done()
                        break

                    progress = h.file_progress()
                    prog = 0
                    for f in file_data:
                        prog += progress[f['idx']]
                    add = prog - old
                    old = prog

                    # Looks like libtorrent return 1 peer randomly
                    # Next num_peers is again back to 0
                    # So only after three consecutive num_peers > 0
                    # we assume peer is available.
                    if s.num_peers > 0:
                        if non_zero_peer_count == 3:
                            start = end
                        else:
                            non_zero_peer_count += 1
                    else:
                        non_zero_peer_count = 0

                    if add > 0 or (s.progress is not None and
                                   s.progress > torrent_last_download):
                        last_download = end
                        if s.progress is not None:
                            torrent_last_download = s.progress

                    pbar.update(add)
                    wait_desc = ''
                    if s.num_peers > 0:
                        if end - last_download > 10:
                            wait_desc = f' {int(end-last_download)} secs download wait,'
                            icon = Downloader.ICON_WAITING_FOR_DOWNLOAD
                    else:
                        if end - start > 10:
                            wait_desc = f' {int(end-start)} secs peer wait,'
                            icon = TorrentDownloader.ICON_WAITING_FOR_PEERS
                        else:
                            icon = Downloader.ICON_WAITING_FOR_DOWNLOAD
                    full_desc = base_desc.format(
                        s.state, s.num_peers, wait_desc, ' P' if s.paused else '', s.progress)
                    pbar.set_rotating_description_str(
                        full_desc, prefix=prefix, icon=icon)

                    await asyncio.sleep(0.5)

            count += 1

    def init_libtorrent(self):
        """Inits libtorrent session"""
        self.lt_session = libtorrent.session(
            {'listen_interfaces': f'0.0.0.0:{self.config['port']}'})
        settings = self.lt_session.get_settings()
        settings['alert_mask'] = (
            libtorrent.alert.category_t.error_notification |
            libtorrent.alert.category_t.performance_warning |
            libtorrent.alert.category_t.progress_notification)
        self.lt_session.apply_settings(settings)

    def get_rpc_client(self):
        """Gets the RPC client"""

        connector = aiohttp.UnixConnector(path=SOCK_FILE)
        # "localhost" is a placeholder, actual connection is via socket
        client = ServerProxy("http://localhost/rpc", connector=connector)
        return client

    async def download(self, urls):
        """Starts download for a bunch of urls"""

        if not os.path.exists(SOCK_FILE):
            return await self.self_download(urls)

        client = self.get_rpc_client()

        try:
            r = await client.ping()
            if r == 'pong':
                print(':: Identified an already executing torrent downloader')
                return await self.rpc_download(urls, client)
        except Exception:
            return await self.self_download(urls)
        finally:
            await client.close()

        return None

    async def self_download(self, urls):
        """Start RPC server and download"""

        self.pending_urls.update(urls)
        asyncio.get_event_loop().create_task(self.start_rpc())
        return await self.local_download(urls)

    @abstractmethod
    async def rpc_download(self, urls, client):
        """Start RPC download"""

    async def process_torrent(self, path, torrent, url, files, save_path, assume_downloaded):
        """RPC call exposed to process torrent from another process"""
        if self.queue is None:
            message = 'Download queue not found. Unexpected error!!'
            print(message)

        self.pending_urls.add(url)
        self.add_torrent_url_mapping(torrent, url)
        for f in files:
            self.torrent_file_url_mapping[f'{torrent}-{f[0]}'] = url
            self.final_results[f'{torrent}-{f[1]}'] = {
                'file': f[1],
                'downloaded': False
            }

        results = []
        res = await self.add_torrent(results, path, torrent, files, save_path, assume_downloaded)

        if not res:
            if url in self.url_errors:
                return f'Torrent {torrent} processing error: {self.url_errors[url]}'
            return f'Torrent {torrent} could not be added. Check alternate process for details.'

        await self.queue.put(results[0])
        return f'{torrent} added successfully'

    @abstractmethod
    async def local_download(self, urls):
        """Starts download without using RPC"""

    async def status(self):
        """Status of RPC server"""

        client = self.get_rpc_client()

        try:
            r = await client.ping()
            if r == 'pong':
                print(':: Remote RPC service is running')
                return
        except Exception:
            pass
        finally:
            await client.close()

        print(':: Remote RPC service is not running.')


class AnnasDownloader(TorrentDownloader):
    """Annas Archive downloader class"""

    NAME = 'annas'

    def __init__(self, args):
        my_config = {
            'torrent_cache_file': args.torrent_cache_file,
            'title_as_filename': not args.no_title_as_filename,
            'guess_extension': not args.no_guess_extension,
            'assume_downloaded': args.assume_downloaded,
            'peer_wait_time': args.peer_wait_time,
        }
        super().__init__(args, my_config)

    def get_cache_file(self):
        return self.config['torrent_cache_file']

    async def process_content(self, response: aiohttp.ClientResponse):
        """
        Process content from html page
        """

        title_as_filename = self.config['title_as_filename']
        guess_extension = self.config['guess_extension']

        tree = html.fromstring(await response.text())
        url = str(response.url)

        if 'Bulk torrents not yet available for this file.' in \
                tree.xpath(BOOK_XPATHS["torrent_exists"]):
            raise ValueError('no torrent is available')

        fname = tree.xpath(BOOK_XPATHS["filename_within_torrent"])[
            0].split('“', 1)[1][:-1]
        t_url = tree.xpath(BOOK_XPATHS["torrent_url"])[0]
        torrent = tree.xpath(BOOK_XPATHS["torrent"])[0][:-1][1:]

        extension = tree.xpath(BOOK_XPATHS["extension"])[0].split(', ')[1]
        title = tree.xpath(BOOK_XPATHS["title"])[0].translate(replace_chars)

        save_as = title if title_as_filename else fname
        if guess_extension:
            save_as += extension
        aa = url.split("/")

        return (f"{aa[0]}//{aa[2]}{str(t_url)}", torrent, fname, save_as)

    async def get_file_index(self):
        """Get index of file value in result tuple"""
        return 3

    async def get_uniq_value(self, key: str):
        """Get string that will make filename unique in case of clash"""
        return key.split('/')[-1]

    async def get_result_uniq_key(self, key: str, result):
        """Get string that is used in final_result"""
        return result[1]

    def get_search_item(self, o):
        """Search item from html tree"""

        link = 'https://annas-archive.org/' + o.xpath("./@href")[0]
        text = o.xpath("./div/h3/text()")[0]
        desc = o.xpath("./div[2]/div/text()")[0]

        return (link, text, desc)

    async def get_search_items(self, session, url):
        """Parses page and get results"""

        results = []
        page_count = 0

        async with session.get(url) as response:
            try:
                tree = html.fromstring(await response.text())
                for o in tree.xpath("//div[@id='aarecord-list']/div/a"):
                    results.append(self.get_search_item(o))

                for o in tree.xpath("//div[@id='aarecord-list']/div[contains(@class, 'js-scroll-hidden')]"):
                    t = f'{o[0]}'
                    p = re.sub(r'-->.*', '', re.sub(r'.*<!--',
                               '', t.replace('\n', '')))
                    o = html.fromstring(p).xpath('//a')[0]
                    results.append(self.get_search_item(o))

                page_count = int(tree.xpath(
                    "//nav[@aria-label='Pagination']/a/@href")[-2].split('=')[-1])

            except IndexError as e:
                print(f'Exception: {e}')

        return results, page_count

    def get_search_url(self):
        """Get url to use for searching"""
        return 'https://annas-archive.org/search?q='

    def get_list_page_url(self, base_url: str, page: int):
        """ Get url of list page given base url"""
        return f'{base_url}&page={page}'

    async def download_common(self, urls):
        """Common code for download"""

        torrents = await self.get_details(urls, {})

        torrent_urls = {}
        for url, torrent in torrents.items():
            self.torrent_file_url_mapping[f'{torrent[1]}-{torrent[2]}'] = url
            self.add_torrent_url_mapping(torrent[1], url)
            torrent_urls.setdefault(
                torrent[0], (torrent[0], torrent[1], url, []))
            torrent_urls[torrent[0]][3].append((torrent[2], torrent[3],))

        return torrent_urls.values()

    async def local_download(self, urls):
        """Starts download for a bunch of urls without RPC"""
        torrent_urls = await self.download_common(urls)
        torrent_handles = await self.download_torrent(torrent_urls, {})
        try:
            await self.process_downloading(torrent_handles, {
                'parallelism': self.config['parallelism']})
        finally:
            self.save_download_results()
            self.show_download_results()

    async def rpc_download(self, urls, client):
        """Start RPC download"""
        torrent_urls = await self.download_common(urls)
        options = {
            'is_rpc': True,
            'rpc_client': client,
        }
        results = await self.download_torrent(torrent_urls, options)
        print()
        if results:
            print(':: RPC add torrent results')
            for r in results:
                print(f'::: {r}')

        if self.url_errors:
            print(':: Errors were seen while processing some of the url(s):')
            for u, err in self.url_errors.items():
                print(f'::: {u} ({err})')


class ArchiveOrgDownloader(HttpDownloader):
    """Archive.Org downloader class implementation"""

    NAME = 'archive'

    def __init__(self, args):
        my_config = {
            'cache_file': args.cache_file,
            'include_all_results': args.include_all_results if hasattr(args, 'include_all_results') else False,
            'rate_limit': args.rate_limit if hasattr(args, 'rate_limit') else ARCHIVE_ORG_API_LIMIT
        }
        super().__init__(args, my_config)
        # self.config['parallelism'] = 5
        self.rate_limiter = RateLimiter(self.config['rate_limit'])

    def get_cache_file(self):
        return self.config['cache_file']

    async def get_details_worker(self, position: int, q: asyncio.Queue, results, options: Dict[str, Any]):
        """Get details required to download the given item"""

        while True:
            arch_id = await q.get()
            url = f'https://archive.org/details/{arch_id}'
            # print(f':: Getting item details for {arch_id} ...')
            await self.rate_limiter.acquire()
            item = await asyncio.to_thread(get_item, arch_id)
            title = item.metadata['title']

            if 'access-restricted-item' in item.metadata and item.metadata['access-restricted-item'] == 'true':
                self.pending_urls.add(url)
                self.url_errors[url] = 'access to item is restricted'
                q.task_done()
                continue

            # print(f'Getting file details for {arch_id} ...')
            await self.rate_limiter.acquire()
            fs = await asyncio.to_thread(get_files, arch_id)
            pdf = None
            pdf_text = None
            for f in fs:
                if f.format == 'Additional Text PDF' or f.format == 'Text PDF':
                    pdf_text = f
                elif f.format == 'Image Container PDF':
                    pdf = f

            if pdf_text is None and pdf is None:
                self.pending_urls.add(url)
                self.url_errors[url] = 'no pdf could be found'
                q.task_done()
                continue

            if pdf_text is not None:
                pdf = pdf_text

            results[arch_id] = (
                arch_id,
                title,
                pdf.name,
                pdf.url,
                pdf.size,
                pdf.sha1,
            )

            # print(f'Got details for {arch_id}.')
            q.task_done()

    async def get_file_index(self):
        """Get index of file value in result tuple"""
        return 1

    async def get_uniq_value(self, key: str):
        """Get string that will make filename unique in case of clash"""
        return key

    async def get_result_uniq_key(self, key: str, result):
        """Get string that is used in final_result"""
        return key

    def get_download_info(self, save_path, data):
        """Get information about download"""

        arch_id, title, _, url, size, sha1 = data
        if url is not None:
            path = f'{save_path}/{title}-{re.sub(r"\W+", '', arch_id)}.pdf'
        else:
            path = None

        return arch_id, path, url, title, size, sha1, None

    async def search(self, text):
        """
        Searches archive.org for user text and downloads preferred matching items.
        """

        search_str = ' '.join(text)
        print(f':: Searching "{search_str}"')
        session = ArchiveSession()
        original_results = search.Search(session, search_str,
                                         fields=['identifier',
                                                 'title', 'publisher'],
                                         sorts=['title asc'])
        print(f':: Original result count: {len(original_results)}')

        results = []

        low_text = [t.lower() for t in text]
        for r in original_results:
            low_title = r['title'].lower() if isinstance(
                r, str) else ''.join(r['title']).lower()
            if not self.config['include_all_results'] and not all(t in low_title for t in low_text):
                continue

            results.append((r['identifier'], r['title'],))

        results = sorted(results, key=lambda x: x[1])

        print(f':: Filtered result count: {len(results)}')

        if not results:
            print('No results found. Exiting')
            return

        await self.process_search_results(results)

    async def download(self, urls):
        """Starts download for a bunch of urls"""

        print(
            f':: Working with rate limit of {self.config["rate_limit"]} api calls per minute')

        for url in urls:
            if '/' in url and not url.startswith("https://archive.org/details/"):
                print(
                    f"{url} --> Invalid url. URL must starts with \"https://archive.org/details/\"")
                exit(1)

        ids = [url.split("/")[4] if '/' in url else url for url in urls]

        arch_items = await self.get_details(ids, {})

        for arch_id, title, _, _, size, _ in arch_items.values():
            self.pending_urls.add(arch_id)
            if size is not None:
                # size is None if book requires borrow
                self.stats['data_size'] += size
                self.final_results[arch_id] = {
                    'file': title,
                    'downloaded': False
                }

        await self.process_downloading(arch_items.values(), {'parallelism': 1})
        self.save_download_results()
        self.show_download_results()


class IndianCultureDownloader(SourceHtmlPageDownloader, HttpDownloader):
    """indianculture.gov.in downloader class implementation"""

    NAME = 'indian-culture'
    BASE_URL = 'https://indianculture.gov.in'

    def __init__(self, args):
        my_config = {
            'cache_file': args.cache_file,
            'no_save_metadata': args.no_save_metadata,
            'skip_download': args.skip_download,
            'metadata_file': args.metadata_file,
        }
        super().__init__(args, my_config)

    def get_cache_file(self):
        return self.config['cache_file']

    def get_search_item(self, o):
        """Search item from html tree"""

        url = o.xpath("./a/@href")[0]
        text = re.sub(r'\s+', ' ', o.xpath("./@title")[0]).strip()

        link = f'{self.BASE_URL}{url}'
        return (link, text)

    async def get_search_items(self, session, url):
        """Parses page and get results"""

        results = []
        page_count = 0

        async with session.get(url) as response:
            try:
                tree = html.fromstring(await response.text())
                base = tree.xpath(
                    "/html/body/div/div[@id='content-inner']/section/div[@class='container']/div/div[@class='row']/section[@id='product']/div/div/div/div")[0]
                for o in base.xpath("./div/div[@class='row']"):
                    for m in o.xpath("./div/div/h2"):
                        results.append(self.get_search_item(m))

                # page count is 0 indexed in source html, hence incrementing
                nav_link = base.xpath(
                    "./nav/ul/li[contains(@class, 'pager__item--last')]/a/@href")
                if nav_link:
                    page_count = 1 + int(nav_link[0].split('=')[-1])

            except IndexError as e:
                print(f'Exception: {e}')

        return results, page_count

    def get_search_url(self):
        """Get url to use for searching"""
        return f'{self.BASE_URL}/indian-culture-repository?search_api_fulltext='

    def get_list_page_url(self, base_url: str, page: int):
        """ Get url of list page given base url"""
        # page number is zero indexed hence subtracting 1
        if '?' in base_url:
            # Search page case
            return f'{base_url}&page={page-1}'
        else:
            # Listing page case
            return f'{base_url}?page={page-1}'

    async def get_file_index(self):
        """Get index of file value in result tuple"""
        return 1

    async def get_uniq_value(self, key: str):
        """Get string that will make filename unique in case of clash"""
        return key.split('/')[-1]

    async def get_result_uniq_key(self, key: str, result):
        """Get string that is used in final_result"""
        return key

    async def process_content(self, response: aiohttp.ClientResponse):
        """
        Process content from html page
        """

        tree = html.fromstring(await response.text())
        url = str(response.url)

        base = tree.xpath(
            "/html/body/div/section/div[@class='container']/div/div[@class='row']/section[@id='product']/div/div[@id='block-mainpagecontent']/div/div/div[@class='row']/div")
        if not base:
            raise ValueError('no pdf could be found')

        base = base[0]
        referer = base.xpath("//div[@id='div0']/iframe/@src")[0]
        pdf_url = referer.split('=')[-1]
        pdf_url = urllib.parse.unquote_plus(pdf_url)
        referer = f'{self.BASE_URL}{referer}'
        title = re.sub(r'\W+', ' ', base.xpath("//h1/span/text()")[0].strip())
        file_no = base.xpath(
            "//p[@class='textdetails']/b[contains(text(), 'File No')]/../text()")
        if file_no:
            file_no = file_no[0].strip()
        else:
            file_no = title
        file_size = base.xpath(
            "//p[@class='textdetails']/b[contains(text(), 'File Size')]/../text()")
        if file_size:
            file_size = file_size[0].strip()
            file_size = int(float(file_size) * 1024 * 1024)
        else:
            file_size = 0

        return pdf_url, file_no, file_size, url, referer, title

    def get_download_info(self, save_path, data):
        """Get information about download"""

        pdf_url, file_no, file_size, url, referer, _ = data

        if url is not None:
            path = f'{save_path}/{file_no}.pdf'
        else:
            path = None

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:139.0) Gecko/20100101 Firefox/139.0',
            'Referer': referer,
        }

        return url, path, pdf_url, file_no, file_size, None, headers

    async def download(self, urls):
        """Starts download for a bunch of urls"""
        for url in urls:
            self.pending_urls.add(url)

        items = await self.get_details(urls, {})

        for _, values in items.items():
            size = values[2]
            self.stats['data_size'] += size
            self.final_results[values[3]] = {
                'file': values[1],
                'downloaded': False
            }

        if not self.config['no_save_metadata']:
            metadata = {}
            for value in items.values():
                metadata[value[1]] = value[5]
            with open(f"{self.config['save_path']}/{self.config['metadata_file']}", 'w', encoding='utf-8') as f:
                f.write(json.dumps(metadata, indent=4))

        if not self.config['skip_download']:
            options = {'parallelism': 1}
            await self.process_downloading(items.values(), options)

        self.save_download_results()
        self.show_download_results()

    async def download_list(self, urls):
        """Download from list of items"""

        print(':: Due to server limitations file download resume is not possible')

        for url in urls:
            url = url.split('?')[0]
            results = await self.get_listing_items(url)

        urls_next = [r[0] for r in results]
        await self.download(urls_next)


class TelegramDownloader(Downloader):
    """Downloads books from telegram"""

    NAME = 'telegram'

    def __init__(self, args):
        my_config = {
            'api_id': args.api_id,
            'api_hash': args.api_hash,
            'phone_number': args.phone_number,
            'channel_username': args.channel_username if hasattr(args, 'channel_username') else None,
            'session_name': args.session_name,
        }
        super().__init__(args, my_config)
        self.client = None
        self.channel_entity = None

    async def init_session(self):
        """Initialize telegram session and channel entity"""

        if self.client is not None:
            return

        session_name = self.config['session_name']
        api_id = self.config['api_id']
        api_hash = self.config['api_hash']
        phone_number = self.config['phone_number']

        self.client = TelegramClient(session_name, api_id, api_hash)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.send_code_request(phone_number)
            await self.client.sign_in(phone_number, input('Enter the code as received in telegram: '))
            print(':: Login successful')

    async def init_channel_entity(self):
        """Initialize channel entity"""

        if self.channel_entity is not None:
            return

        self.channel_entity = await self.client.get_entity(self.config['channel_username'])

    async def download_from_message(self, message, position: int):
        """Download pdf from message"""

        try:
            if not self.is_useful_message(message):
                raise ValueError('no pdf found in the message')

            message_id, size, title = self.get_message_info(message)

            with TqdmDownloadHandler(position=position, unit='B', unit_scale=True,
                                     leave=False, unit_divisor=1024, miniters=1,
                                     total=size) as pbar:
                last = 0

                def handle_download(current, total):
                    nonlocal last, pbar, title
                    pbar.set_rotating_description_str(f'Downloading {title}')
                    pbar.update(current-last)
                    pbar.total = total
                    last = current

                file_name = f"{self.config['save_path']}/"
                if title:
                    file_name += f'{title}'

                if not await self.is_file_already_downloaded(file_name):
                    await self.client.download_media(message, file=file_name,
                                                     progress_callback=handle_download)
                    await self.save_file_hash(file_name)
                await self.complete_single_file_download(message_id, size)

        except ValueError as e:
            self.url_errors[message.id] = str(e)

    def is_useful_message(self, message):
        """Is message useful for us"""
        return message.media \
            and isinstance(message.media, MessageMediaDocument) \
            and message.media.document \
            and message.document.mime_type == 'application/pdf'

    def get_message_info(self, message):
        """Get message info"""

        title = ''
        for attribute in message.media.document.attributes:
            if isinstance(attribute, DocumentAttributeFilename):
                title = attribute.file_name
            break

        return message.id, message.media.document.size, title

    async def get_items(self, channel_entity, search_text: str = ''):
        """Get all items from channel"""

        results = []
        search_text = search_text.lower()

        async for message in self.client.iter_messages(
                channel_entity,
                filter=MessageMediaDocument):
            if not self.is_useful_message(message):
                continue

            _, size, title = self.get_message_info(message)
            low_title = title.lower()
            if not title or search_text not in low_title:
                continue

            results.append((message.id, f'{title} {size}'))

        return results

    async def process_download_worker(self, position: int, q: asyncio.Queue,
                                      options: Dict[str, Any]):
        """Not implemented required"""

        while True:
            message = await q.get()
            await self.download_from_message(message, position)
            q.task_done()

    async def search(self, text):
        """
        Searches archive.org for user text and downloads preferred matching items.
        """

        search_str = ' '.join(text)
        print(f':: Searching for files with name "{search_str}"')

        print(':: Due to server limitations file download resume is not possible')

        try:
            await self.init_session()
            await self.init_channel_entity()

            results = await self.get_items(self.channel_entity, search_str)

            if not results:
                print('No results found. Exiting')
                return

            await self.process_search_results(results)
        finally:
            await self.client.disconnect()

    async def download(self, urls):
        """Starts download for a bunch of urls"""

        try:
            await self.init_session()
            await self.init_channel_entity()

            messages = await self.client.get_messages(self.channel_entity, ids=list(urls))

            downloadables = []
            for message in messages:
                if not self.is_useful_message(message):
                    continue

                message_id, size, title = self.get_message_info(message)
                self.pending_urls.add(message_id)
                self.stats['data_size'] += size
                self.final_results[message_id] = {
                    'file': title,
                    'downloaded': False,
                }
                downloadables.append(message)

            await self.process_downloading(downloadables, {'parallelism': self.config['parallelism']})
            self.save_download_results()
            self.show_download_results()
        finally:
            await self.client.disconnect()


def parse_args_common(parser, cache: bool = True):
    """Defines common command arguments"""

    parser.add_argument('-s', '--save-path', type=str, default='.',
                        help='Save path (default: current directory)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help=f'Timeout for networking operations (default={DEFAULT_TIMEOUT})')
    parser.add_argument('-n', '--parallelism', type=int, default=DEFAULT_PARALLELISM,
                        help='Number of parallel downloads. '
                        'Only useful if more than one url is specified'
                        f' (default: {DEFAULT_PARALLELISM})')
    if cache:
        parser.add_argument('--skip-cache', action='store_true',
                            help='Skip usage of cache (by default cache is used)')
    parser.add_argument('--temp-file-path', type=str, default=tempfile.gettempdir(),
                        help=f'Temp directory (defaults to folder in {tempfile.gettempdir()})')


def parse_args_command(parser, list_supported: bool = False,
                       status_supported: bool = False, download_text: str = 'URL'):
    """Create parser for sub commands"""

    commands = parser.add_subparsers(
        dest='subcommand', help='Available sub commands', required=True)

    _list = None
    if list_supported:
        _list = commands.add_parser(
            'list', help='Downloads items from given listing')
        _list.add_argument('urls', type=str, nargs='+',
                           help='Listing URL(s) to download from')

    _search = commands.add_parser(
        'search', help='Search and download preferred matching items')
    _search.add_argument(
        'text', type=str, nargs='+', help='Text to be searched')
    _search.add_argument('--download-all', action='store_true',
                         help='Use this option to download all results from search automatically.'
                         ' By default you are prompted for selection.')
    _download = commands.add_parser(
        'download', help='Downloads items from given urls')
    _download.add_argument('urls', type=str, nargs='+',
                           help=f'{download_text}(s) to download')

    if status_supported:
        commands.add_parser(
            'status', help='Get status of running downloader via RPC')

    return _list, _search, _download


def parse_args():
    """Parses command line arguments"""

    parser = argparse.ArgumentParser(
        description='pystak - a tool to download books from various sources')

    commands = parser.add_subparsers(
        dest='command', help='Available commands', required=True)

    aa = commands.add_parser(AnnasDownloader.NAME,
                             help='Annas archive downloader')
    parse_args_common(aa)
    aa.add_argument('-c', '--torrent-cache-file', type=str, default=DEFAULT_TORRENT_CACHE,
                    help=f'Cache file for storing torrent data (default: {DEFAULT_TORRENT_CACHE})')
    aa.add_argument('-t', '--no-title-as-filename', action='store_true',
                    help='Avoids using listing title as filename (default: assume title as file name)')
    aa.add_argument('-g', '--no-guess-extension', action='store_true',
                    help='Avoids guessing of extension from listing (default: guess extension)')
    aa.add_argument('--assume-downloaded', action='store_true',
                    help='Assumes that file is downloaded if the file exists at save-path (default: do not assume)')
    aa.add_argument('--peer-wait-time', type=int, default=DEFAULT_PEER_WAIT_TIME,
                    help=f'Wait time for peer availability for torrent (default: {DEFAULT_PEER_WAIT_TIME})')
    aa.add_argument('-p', '--port', type=int, default=DEFAULT_PORT,
                    help=f'Port to use for libtorrent (default: {DEFAULT_PORT})')
    parse_args_command(aa, status_supported=True)

    archive = commands.add_parser(
        ArchiveOrgDownloader.NAME, help='Archive.org downloader')
    parse_args_common(archive)
    archive.add_argument('-c', '--cache-file', type=str, default=DEFAULT_ARCHIVE_CACHE,
                         help=f'Cache file for storing archive.org data (default: {DEFAULT_ARCHIVE_CACHE})')

    _, _search, _download = parse_args_command(archive)
    _search.add_argument('--include-all-results', action='store_true',
                         help='Use this option to prevent filtering over the results'
                         'returned by archive.org (default: True). By'
                         'default archive.org return too many items')
    _download.add_argument('--rate-limit', type=int, default=ARCHIVE_ORG_API_LIMIT,
                           help=f'Maximum number of API calls to be made per minute (defaulte{ARCHIVE_ORG_API_LIMIT})')

    ind_cult = commands.add_parser(
        IndianCultureDownloader.NAME, help='indianculture.gov.in downloader')
    parse_args_common(ind_cult)
    ind_cult.add_argument('-c', '--cache-file', type=str, default=DEFAULT_IND_CULT_CACHE,
                          help=f'Cache file for storing indianculture.gov.in data (default: {DEFAULT_IND_CULT_CACHE})')
    ind_cult.add_argument('--no-save-metadata', action='store_true',
                          help='Do not save metadata associated with downloaded files (default is to save metadata)')
    ind_cult.add_argument('--skip-download', action='store_true',
                          help='Skip axtual downloading of files (default is to download). Only useful to if you just want to save metadata')
    ind_cult.add_argument('--metadata-file', type=str, default=DEFAULT_INT_CULT_METADATA,
                          help=f'Metadat file for storing metadata of files downloaded (default: {DEFAULT_INT_CULT_METADATA})')
    parse_args_command(ind_cult, list_supported=True)

    telegram = commands.add_parser(
        TelegramDownloader.NAME, help='Telegram channel downloader')
    parse_args_common(telegram, False)
    telegram.add_argument('-i', '--api-id', type=str, required=True,
                          help='Your telegram API Id')
    telegram.add_argument('-a', '--api-hash', type=str, required=True,
                          help='Your telegram API hash')
    telegram.add_argument('-p', '--phone-number', type=str, required=True,
                          help='Telegram phone number')
    telegram.add_argument('--session-name', type=str, default=DEFAULT_TELEGRAM_SESSION,
                          help=f'Telegram session name (default is {DEFAULT_TELEGRAM_SESSION})')
    _, _search, _download = parse_args_command(
        telegram, download_text='Message')
    _search.add_argument('-c', '--channel-username', type=str, required=True,
                         help='Telegram channel username')
    _download.add_argument('-c', '--channel-username', type=str, required=True,
                           help='Telegram channel username')

    _rerun = commands.add_parser(
        'rerun', help='Rerun a run from supplied run-end*.txt file')
    _rerun.add_argument('files', type=str, nargs='+',
                        help='run-end*.txt file(s) to rerun')

    return parser.parse_args()


async def rerun(args):
    """Rerun past runs"""

    for file_path in args.files:
        if not os.path.exists(file_path):
            print(f':: file not found {file_path}')
            os.sys.exit(1)

    for file_path in args.files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
                run = json_data['run']
                config = json_data['config']
                pending_urls = json_data['pending_urls']

                if run == AnnasDownloader.NAME:
                    command = AnnasDownloader
                elif run == ArchiveOrgDownloader.NAME:
                    command = ArchiveOrgDownloader
                elif run == IndianCultureDownloader.NAME:
                    command = IndianCultureDownloader
                elif run == TelegramDownloader.NAME:
                    command = TelegramDownloader
                else:
                    raise ValueError(f'invalid run type: {run}')

                args = argparse.Namespace(**config)
                await command(args).download(pending_urls)

        except (FileNotFoundError, PermissionError, IOError, IsADirectoryError, ValueError, json.JSONDecodeError) as e:
            print(f':: Failed to process {file_path}: {e}')


async def main():
    """The main function"""

    args = parse_args()

    if args.command == 'rerun':
        return await rerun(args)

    data = {
        AnnasDownloader.NAME: (AnnasDownloader, True, False),
        ArchiveOrgDownloader.NAME: (ArchiveOrgDownloader, False, False),
        IndianCultureDownloader.NAME: (IndianCultureDownloader, False, True),
        TelegramDownloader.NAME: (TelegramDownloader, False, False),
    }

    command, has_status, has_list = data[args.command]
    obj = command(args)
    if args.subcommand == 'search':
        await obj.search(args.text)
    elif args.subcommand == 'download':
        await obj.download(args.urls)
    elif has_status and args.subcommand == 'status':
        await obj.status()
    elif has_list and args.subcommand == 'list':
        await obj.download_list(args.urls)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
