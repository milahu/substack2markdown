import argparse
import json
import os
import io
import re
import base64
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, unquote
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep
import asyncio
import atexit
import signal

import html2text
import markdown
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium_driverless import webdriver
from selenium_driverless.types.by import By

USE_PREMIUM: bool = True  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.thefitzwilliam.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = "substack_md_files"  # Name of the directory we'll save the .md essay files
BASE_HTML_DIR: str = "substack_html_pages"  # Name of the directory we'll save the .html essay files
BASE_IMAGE_DIR: str = "substack_images"
ASSETS_DIR: str = os.path.dirname(__file__) + "/assets"
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts


def count_images_in_markdown(md_content: str) -> int:
    """Count number of Substack CDN image URLs in markdown content."""
    # [![](https://substackcdn.com/image/fetch/x.png)](https://substackcdn.com/image/fetch/x.png)
    # regex lookahead: match "...)" but not "...)]" suffix
    pattern = re.compile(r'\(https://substackcdn\.com/image/fetch/[^\s\)]+\)(?=[^\]]|$)')
    matches = re.findall(pattern, md_content)
    return len(matches)


def sanitize_image_filename(url: str) -> str:
    """Create a safe filename from URL or content."""
    # Extract original filename from CDN URL
    if "substackcdn.com" in url:
        # Get the actual image URL after the CDN parameters
        original_url = unquote(url.split("/https%3A%2F%2F")[1])
        filename = original_url.split("/")[-1]
    else:
        filename = url.split("/")[-1]

    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)

    # If filename is too long or empty, create hash-based name
    if len(filename) > 100 or not filename:
        hash_object = hashlib.md5(url.encode())
        ext = mimetypes.guess_extension(requests.head(url).headers.get('content-type', '')) or '.jpg'
        filename = f"{hash_object.hexdigest()}{ext}"

    return filename


def get_post_slug(url: str) -> str:
    match = re.search(r'/p/([^/]+)', url)
    return match.group(1) if match else 'unknown_post'


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')  # Parse the URL to get the netloc, and split on '.'
    return parts[1] if parts[0] == 'www' else parts[0]  # Return the main part of the domain, while ignoring 'www' if
    # present


def generate_html_file(args, author_name: str) -> None:
    """
    Generates a HTML file for the given author.
    """
    if not os.path.exists(args.html_directory):
        os.makedirs(args.html_directory)

    # Read JSON data
    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    # Convert JSON data to a JSON string for embedding
    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(args.author_template, 'r', encoding='utf-8') as file:
        html_template = file.read()

    # Insert the JSON string into the script tag in the HTML template
    html_with_data = html_template.replace('<!-- AUTHOR_NAME -->', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

    # Write the modified HTML to a new file
    html_output_path = os.path.join(args.html_directory, f'{author_name}.html')
    with open(html_output_path, 'w', encoding='utf-8') as file:
        file.write(html_with_author)


class BaseSubstackScraper(ABC):
    def __await__(self):
        return self._async_init().__await__()

    async def __aenter__(self):
        return await self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def __init__(self, args, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.args = args
        self.base_substack_url: str = base_substack_url

        self.writer_name: str = extract_main_part(base_substack_url)
        md_save_dir: str = f"{md_save_dir}/{self.writer_name}"

        self.md_save_dir: str = md_save_dir
        self.html_save_dir: str = f"{html_save_dir}/{self.writer_name}"

        if not os.path.exists(md_save_dir):
            os.makedirs(md_save_dir)
            print(f"Created md directory {md_save_dir}")
        if not os.path.exists(self.html_save_dir):
            os.makedirs(self.html_save_dir)
            print(f"Created html directory {self.html_save_dir}")

        if not self.args.no_images:
            os.makedirs(self.args.image_directory, exist_ok=True)

        self.keywords: List[str] = ["about", "archive", "podcast"]
        self.post_urls: List[str] = self.get_all_post_urls()

    async def _async_init(self):
        self._loop = asyncio.get_running_loop()
        return self

    def get_all_post_urls(self) -> List[str]:
        """
        Attempts to fetch URLs from sitemap.xml, falling back to feed.xml if necessary.
        """
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def fetch_urls_from_sitemap(self) -> List[str]:
        """
        Fetches URLs from sitemap.xml.
        """
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
        response = requests.get(sitemap_url)

        if not response.ok:
            print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        return urls

    def fetch_urls_from_feed(self) -> List[str]:
        """
        Fetches URLs from feed.xml.
        """
        print('Falling back to feed.xml. This will only contain up to the 22 most recent posts.')
        feed_url = f"{self.base_substack_url}feed.xml"
        response = requests.get(feed_url)

        if not response.ok:
            print(f'Error fetching feed at {feed_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = []
        for item in root.findall('.//item'):
            link = item.find('link')
            if link is not None and link.text:
                urls.append(link.text)

        return urls

    @staticmethod
    def filter_urls(urls: List[str], keywords: List[str]) -> List[str]:
        """
        This method filters out URLs that contain certain keywords
        """
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def html_to_md(html_content: str) -> str:
        """
        This method converts HTML to Markdown
        """
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
        """
        This method saves content to a file. Can be used to save HTML or Markdown
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        # if os.path.exists(filepath):
        if False:
            print(f"File already exists: {filepath}")
            return

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def md_to_html(md_content: str) -> str:
        """
        This method converts Markdown to HTML
        """
        return markdown.markdown(md_content, extensions=['extra'])


    def save_to_html_file(self, filepath: str, content: str) -> None:
        """
        This method saves HTML content to a file with a link to an external CSS file.
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        # Calculate the relative path from the HTML file to the CSS file
        html_dir = os.path.dirname(filepath)
        css_path = os.path.relpath(self.args.assets_dir + "/css/essay-styles.css", html_dir)
        css_path = css_path.replace("\\", "/")  # Ensure forward slashes for web paths

        html_content = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Markdown Content</title>
                <link rel="stylesheet" href="{css_path}">
            </head>
            <body>
                <main class="markdown-content">
                {content}
                </main>
            </body>
            </html>
        """

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(html_content)

    @staticmethod
    def get_filename_from_url(url: str, filetype: str = ".md") -> str:
        """
        Gets the filename from the URL (the ending)
        """
        if not isinstance(url, str):
            raise ValueError("url must be a string")

        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")

        if not filetype.startswith("."):
            filetype = f".{filetype}"

        return url.split("/")[-1] + filetype

    @staticmethod
    def combine_metadata_and_content(title: str, subtitle: str, date: str, like_count: str, content) -> str:
        """
        Combines the title, subtitle, and content into a single string with Markdown format
        """
        if not isinstance(title, str):
            raise ValueError("title must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        metadata = f"# {title}\n\n"
        if subtitle:
            metadata += f"## {subtitle}\n\n"
        metadata += f"**{date}**\n\n"
        metadata += f"**Likes:** {like_count}\n\n"

        return metadata + content

    def extract_post_data(self, soup: BeautifulSoup) -> Tuple[str, str, str, str, str]:
        """
        Converts a Substack post soup to markdown, returning metadata and content.
        Returns (title, subtitle, like_count, date, md_content).
        """
        # Title (sometimes h2 if video present)
        title_element = soup.select_one("h1.post-title, h2")
        title = title_element.text.strip() if title_element else "Untitled"

        # Subtitle
        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        # Date — try CSS selector first
        date = ""
        date_element = soup.select_one("div.pencraft.pc-reset.color-pub-secondary-text-hGQ02T")
        if date_element and date_element.text.strip():
            date = date_element.text.strip()

        # Fallback: JSON-LD metadata
        if not date:
            script_tag = soup.find("script", {"type": "application/ld+json"})
            if script_tag and script_tag.string:
                try:
                    metadata = json.loads(script_tag.string)
                    if "datePublished" in metadata:
                        date_str = metadata["datePublished"]
                        date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        date = date_obj.strftime("%b %d, %Y")
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass

        if not date:
            date = "Date not found"

        # Like count
        like_count_element = soup.select_one("a.post-ufi-button .label")
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )

        # Post content
        content_element = soup.select_one("div.available-content")
        content_html = str(content_element) if content_element else ""
        md = self.html_to_md(content_html)

        # Combine metadata + content
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)

        return title, subtitle, like_count, date, md_content

    async def get_window_preloads(self, soup):
        # all comments are stored in javascript
        # <script>window._preloads = JSON.parse("{\"isEU\":true,\"language\":\"en\",...}")</script>
        # only some comments are rendered in html
        # with buttons to "Expand full comment" and "Load More"
        # see also
        # https://www.selfpublife.com/p/automatically-expand-all-substack-comments
        window_preloads = None
        for script_element in soup.select("script"):
            script_text = script_element.text.strip()
            if not script_text.startswith("window._preloads"):
                continue
            # pos1 = re.search(r'window._preloads\s*=\s*JSON\.parse\(', script_text).span()[1]
            pos1 = script_text.find("(") + 1
            pos2 = script_text.rfind(")")
            window_preloads = json.loads(json.loads(script_text[pos1:pos2]))
            break
        assert window_preloads, f"not found <script>window._preloads...</script> at {url!r}"
        return window_preloads

    def count_comments(self, comments_preloads):

        def count_comments_inner(comment):
            res = 1
            for child_comment in comment["children"]:
                res += count_comments_inner(child_comment)
            return res

        res = 0
        for comment in comments_preloads["initialComments"]:
            res += count_comments_inner(comment)
        return res

    def render_comments_html(self, comments_preloads):

        def render_comment_body(body):
            body = body.strip()
            body = "<p>" + body + "</p>"
            body = body.replace("\n", "</p>\n<p>")
            # TODO more?
            return body

        def render_comments_html_inner(comment, buf):
            assert comment["type"] == "comment", f'unexpected comment type: {comment["type"]!r}'
            buf.write(f'<details class="comment" id="{comment["id"]}" open>\n')
            buf.write(f'<summary>\n')

            # NOTE user IDs are constant, user handles are variable
            # when i change my user handle
            # then other users can use my old user handle
            if not comment["user_id"] is None:
                buf.write(f'<a class="user" href="https://substack.com/profile/{comment["user_id"]}">')

            if not comment["name"] is None:
                buf.write(comment["name"]) # human-readable username
            else:
                # Comment removed
                buf.write("null")

            if not comment["user_id"] is None:
               buf.write('</a>\n')
            else:
               buf.write('\n')

            other_pub = comment["metadata"].get("author_on_other_pub")
            if other_pub:
                # NOTE publication handles are quasi-constant:
                # when i change my publication handle
                # then other users cannot use my old publication handle
                # NOTE "Changing your publication's subdomain
                # does not automatically set up a redirect from the old subdomain to the new one."
                buf.write(f'(<a class="pub" pub-id="{other_pub["id"]}" href="{other_pub["base_url"]}">')
                buf.write(other_pub["name"])
                buf.write('</a>)\n')

            buf.write(comment["date"] + '\n') # "2025-05-17T06:51:39.485Z"

            for reaction, reaction_count in comment["reactions"].items():
                if reaction_count == 0: continue
                buf.write(reaction + str(reaction_count) + '\n') # "❤123"
                # buf.write(str(reaction_count) + reaction + '\n') # "123❤"

            buf.write('</summary>\n')

            buf.write('<blockquote>\n')
            buf.write('\n')

            if comment["body"] is None:
                # Comment removed
                status = comment.get("status")
                if status is None:
                    buf.write('(Comment removed)\n')
                else:
                    # "moderator_removed", ...
                    buf.write('(status:' + status + ')\n')
                # TODO comment["bans"]
                # TODO comment["suppressed"]
                # TODO comment["user_banned"]
                # TODO comment["user_banned_for_comment"]
            else:
                buf.write(render_comment_body(comment["body"]) + '\n')

            for child_comment in comment["children"]:
                buf.write('\n')
                render_comments_html_inner(child_comment, buf)
            buf.write('</blockquote>\n')

            buf.write('</details>\n')
            buf.write('\n')

        buf = io.StringIO()
        # NOTE the name "initial" is misleading. all comments are stored in this array
        # NOTE comments are sorted by likes
        for comment in comments_preloads["initialComments"]:
            render_comments_html_inner(comment, buf)
        return buf.getvalue()

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
        """
        Saves essays data to a JSON file for a specific author.
        """
        data_dir = os.path.join(JSON_DATA_DIR)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        json_path = os.path.join(data_dir, f'{self.writer_name}.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as file:
                existing_data = json.load(file)
            essays_data = existing_data + [data for data in essays_data if data not in existing_data]
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(essays_data, f, ensure_ascii=False, indent=4)

    async def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """
        Iterates over all posts and saves them as markdown and html files
        """
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        for url in tqdm(self.post_urls, total=total):
            try:
                md_filename = self.get_filename_from_url(url, filetype=".md")
                html_filename = self.get_filename_from_url(url, filetype=".html")
                md_filepath = os.path.join(self.md_save_dir, md_filename)
                html_filepath = os.path.join(self.html_save_dir, html_filename)

                # if not os.path.exists(md_filepath):
                if True:
                    soup = await self.get_url_soup(url)
                    if soup is None:
                        total += 1
                        continue
                    title, subtitle, like_count, date, md = self.extract_post_data(soup)

                    if not self.args.no_images:
                        total_images = count_images_in_markdown(md)
                        post_slug = get_post_slug(url)
                        with tqdm(total=total_images, desc=f"Downloading images for {post_slug}", leave=False) as img_pbar:
                            md = await self.process_markdown_images(md, self.writer_name, post_slug, img_pbar)

                    comments_html = None
                    comments_num = None
                    if not self.args.no_comments:
                        comments_url = url + "/comments"
                        # comments_url = "https://willstorr.substack.com/p/scamming-substack/comments" # test
                        comments_soup = await self.get_url_soup(comments_url)
                        comments_preloads = await self.get_window_preloads(comments_soup)
                        if 0:
                            # debug
                            # TODO add option to write the original "preloads" data to json files
                            with open("comments_preloads.json", "w") as f:
                                json.dump(comments_preloads, f, indent=2)
                            raise 5
                        comments_num = self.count_comments(comments_preloads)
                        if comments_num > 0:
                            comments_html = self.render_comments_html(comments_preloads)
                            comments_html = (
                                '\n\n' +
                                '<hr>\n' +
                                # this can collide with other elements with id="comments"
                                # '<section id="comments">\n' +
                                '<section class="comments">\n' +
                                '<h2>Comments</h2>\n' +
                                '<details open>\n' +
                                f'<summary>{comments_num} comments</summary>\n' +
                                comments_html + '\n' +
                                '</details>'
                                '</section>'
                            )
                            md += comments_html

                    self.save_to_file(md_filepath, md)

                    # Convert markdown to HTML and save
                    html_content = self.md_to_html(md)
                    self.save_to_html_file(html_filepath, html_content)

                    essays_data.append({
                        "title": title,
                        "subtitle": subtitle,
                        "like_count": like_count,
                        "comment_count": comments_num,
                        "date": date,
                        "file_link": md_filepath,
                        "html_link": html_filepath
                    })
                else:
                    print(f"File already exists: {md_filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
                # raise e # debug
            count += 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(self.args, author_name=self.writer_name)

    async def download_image(
            self,
            url: str,
            save_path: Path,
            pbar: Optional[tqdm] = None
        ) -> Optional[str]:
        """Download image from URL and save to path."""
        try:
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                if pbar:
                    pbar.update(1)
                return str(save_path)
        except Exception as exc:
            if pbar:
                pbar.write(f"Error downloading image {url}: {str(exc)}")
            # raise exc # debug
        return None

    async def process_markdown_images(
            self,
            md_content: str,
            author: str,
            post_slug: str,
            pbar=None
        ) -> str:
        """Process markdown content to download images and update references."""
        image_dir = Path(self.args.image_directory) / author / post_slug
        # [![](https://substackcdn.com/image/fetch/x.png)](https://substackcdn.com/image/fetch/x.png)
        pattern = re.compile(r'\(https://substackcdn\.com/image/fetch/[^\s\)]+\)')
        buf = io.StringIO()
        last_end = 0
        for match in pattern.finditer(md_content):
            buf.write(md_content[last_end:match.start()])
            url = match.group(0).strip("()")
            filename = sanitize_image_filename(url)
            save_path = image_dir / filename
            if not save_path.exists():
                await self.download_image(url, save_path, pbar)
            rel_path = os.path.relpath(save_path, Path(self.args.directory) / author)
            buf.write(f"({rel_path})")
            last_end = match.end()
        buf.write(md_content[last_end:])
        return buf.getvalue()


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, args, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        super().__init__(args, base_substack_url, md_save_dir, html_save_dir)

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Gets soup from URL using requests
        """
        try:
            page = requests.get(url, headers=None)
            soup = BeautifulSoup(page.content, "html.parser")
            if soup.find("h2", class_="paywall-title"):
                print(f"Skipping premium article: {url}")
                return None
            return soup
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        args,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        headless: bool = False,
        chromium_path: str = '',
        user_agent: str = ''
    ) -> None:
        super().__init__(args, base_substack_url, md_save_dir, html_save_dir)

        self.driver = None

        def exit_handler(signum, frame):
            print()
            print(f"exit_handler: received signal {signum}")
            try:
                asyncio.get_event_loop().create_task(self._cleanup_sync())
            except Exception:
                pass
            raise SystemExit(0)

        signal.signal(signal.SIGINT, exit_handler)
        signal.signal(signal.SIGTERM, exit_handler)

        atexit.register(self._cleanup_sync)

        options = webdriver.ChromeOptions()
        self.chrome_options = options
        if headless:
            # modern headless flag (works better with recent Chromium)
            options.add_argument("--headless=new")
        if chromium_path:
            options.binary_location = chromium_path
        if user_agent:
            options.add_argument(f"user-agent={user_agent}")

    async def _async_init(self):
        self._loop = asyncio.get_running_loop()

        await self._start_driver()
        await self.login()
        return self

    async def _start_driver(self):
        self.driver = await webdriver.Chrome(options=self.chrome_options)

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self) -> None:
        if self.driver:
            await self.driver.quit()

    def _cleanup_sync(self):
        try:
            if not self.driver:
                return
            proc = self.driver._process
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    proc.kill()
        except Exception as exc:
            print("_cleanup_sync failed:", exc)

    async def login(self):
        await self.driver.get("https://substack.com/sign-in")
        await asyncio.sleep(2)

        signin = await self.driver.find_element(
            By.XPATH, "//a[contains(@class,'login-option')]"
        )
        await signin.click()

        await asyncio.sleep(2)

        email = await self.driver.find_element(By.NAME, "email")
        password = await self.driver.find_element(By.NAME, "password")

        await email.send_keys(self.args.email)
        await password.send_keys(self.args.password)

        submit = await self.driver.find_element(
            By.XPATH, "//*[@id='substack-login']//form//button"
        )
        await submit.click()

        await asyncio.sleep(8)

        if await self.is_login_failed():
            raise RuntimeError("Substack login failed")

    async def is_login_failed(self):
        """
        Check for the presence of the 'error-container' to indicate a failed login attempt.
        """
        elements = await self.driver.find_elements(By.ID, "error-container")
        return bool(elements)

    async def get_url_soup(self, url: str):
        """
        Gets soup from URL using logged in selenium driver
        """
        await self.driver.get(url)
        html = await self.driver.page_source
        return BeautifulSoup(html, "html.parser")

    async def download_image_FIXME(
            self,
            url: str,
            save_path: Path,
            pbar: Optional[tqdm] = None
        ) -> Optional[str]:
        """Download image using selenium_driverless"""

        # NOTE for now this works with the default "def download_image"

        # WONTFIX "fetch" fails due to CORS policy

        # WONTFIX "canvas" does not return the original image bytes

        # we could fetch images with CDP Network.getResponseBody
        # but that requires lots of boilerplate code
        # fix: use https://github.com/milahu/aiohttp_chromium

        try:
            # Execute JS fetch inside browser
            result = await self.driver.execute_async_script(
                """
                const url = arguments[0];
                const callback = arguments[arguments.length - 1];

                const img = new Image();
                img.crossOrigin = 'Anonymous'; // try to avoid CORS issues
                img.onload = () => {
                    try {
                        const canvas = document.createElement('canvas');
                        canvas.width = img.width;
                        canvas.height = img.height;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        const dataUrl = canvas.toDataURL('image/png'); // returns "data:image/png;base64,..."
                        const base64 = dataUrl.split(',')[1]; // strip prefix
                        callback({data: base64});
                    } catch (err) {
                        callback({error: err.message, stack: err.stack});
                    }
                };
                img.onerror = (err) => {
                    callback({error: 'Image load error', stack: err.toString()});
                };
                img.src = url;
                """,
                url
            )

            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(f"{result['error']}\nJS stack:\n{result['stack']}")

            # Decode base64 to bytes
            image_bytes = base64.b64decode(result)

            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(image_bytes)

            if pbar:
                pbar.update(1)

            return str(save_path)

        except Exception as exc:
            if pbar:
                pbar.write(f"Error downloading image {url}: {exc}")
            # raise exc # debug
            return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a Substack site.")
    parser.add_argument(
        "--config", type=str, help="JSON config file with email and password."
    )
    parser.add_argument(
        "--email", type=str, help="Login E-Mail."
    )
    parser.add_argument(
        "--password", type=str, help="Login password."
    )
    parser.add_argument(
        "-u",
        "--url", # args.url
        type=str,
        default=BASE_SUBSTACK_URL,
        help="The base URL of the Substack site to scrape."
    )
    parser.add_argument(
        "-d",
        "--directory", # args.directory
        type=str,
        default=BASE_MD_DIR,
        help="The directory to save scraped posts."
    )
    parser.add_argument(
        "-n",
        "--number", # args.number
        type=int,
        default=0,
        help="The number of posts to scrape. If 0 or not provided, all posts will be scraped.",
    )
    parser.add_argument(
        "-p",
        "--premium",
        action="store_true",
        help="Include -p in command to use the Premium Substack Scraper with selenium.",
    )
    parser.add_argument(
        "--assets-dir", # args.assets_dir
        default=ASSETS_DIR,
        help=f"Path to assets directory. Default: {ASSETS_DIR!r}",
    )
    parser.add_argument(
        "--author-template", # args.author_template
        help=f"Path to author_template.html. Default: {repr('{assets_dir}/' + HTML_TEMPLATE)}",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Include -h in command to run browser in headless mode when using the Premium Substack "
        "Scraper.",
    )
    parser.add_argument(
        "--chromium-path", # args.chromium_path
        type=str,
        default="",
        help='Optional: The path to the Chromium browser executable (i.e. "path/to/chromium").',
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="",
        help="Optional: Specify a custom user agent for selenium browser automation. Useful for "
        "passing captcha in headless mode",
    )
    parser.add_argument(
        "--html-directory", # args.html_directory
        type=str,
        default=BASE_HTML_DIR,
        help=f"The directory to save scraped posts as HTML files. Default: {BASE_HTML_DIR!r}",
    )
    parser.add_argument(
        "--image-directory", # args.image_directory
        type=str,
        default=BASE_IMAGE_DIR,
        help=f"The directory to save scraped image files. Default: {BASE_IMAGE_DIR!r}",
    )
    parser.add_argument(
        "--no-images", # args.no_images
        action="store_true",
        help=f"Do not download images.",
    )
    parser.add_argument(
        "--no-comments", # args.no_comments
        action="store_true",
        help=f"Do not download comments.",
    )

    return parser.parse_args()


async def async_main():
    args = parse_args()

    if args.config:
        with open(args.config) as f:
            config = json.load(f)
        args.email = config["email"]
        args.password = config["password"]
        # TODO more

    assert args.email
    assert args.password

    if not args.author_template:
        args.author_template = args.assets_dir + "/" + HTML_TEMPLATE

    if True:
        if args.premium:
            scraper = await PremiumSubstackScraper(
                args=args,
                base_substack_url=args.url,
                headless=args.headless,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )
        else:
            scraper = await SubstackScraper(
                args=args,
                base_substack_url=args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )

        await scraper.scrape_posts(args.number)
        await scraper.close()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
