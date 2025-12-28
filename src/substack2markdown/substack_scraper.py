import argparse
import json
import os
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
from urllib.parse import urlparse

USE_PREMIUM: bool = True  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.thefitzwilliam.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = "substack_md_files"  # Name of the directory we'll save the .md essay files
BASE_HTML_DIR: str = "substack_html_pages"  # Name of the directory we'll save the .html essay files
ASSETS_DIR: str = os.path.dirname(__file__) + "/assets"
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts


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

        if os.path.exists(filepath):
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
        css_path = os.path.relpath(args.assets_dir + "/css/essay-styles.css", html_dir)
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

                    soup = await self.get_url_soup(url)
                    if soup is None:
                        total += 1
                        continue
                    title, subtitle, like_count, date, md = self.extract_post_data(soup)
                    self.save_to_file(md_filepath, md)

                    # Convert markdown to HTML and save
                    html_content = self.md_to_html(md)
                    self.save_to_html_file(html_filepath, html_content)

                    essays_data.append({
                        "title": title,
                        "subtitle": subtitle,
                        "like_count": like_count,
                        "date": date,
                        "file_link": md_filepath,
                        "html_link": html_filepath
                    })
                else:
                    print(f"File already exists: {md_filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
            count += 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(self.args, author_name=self.writer_name)


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
