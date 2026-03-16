"""
Local scraping engine — replaces Apify for non-LinkedIn sources.
Uses only stdlib + feedparser + beautifulsoup4. Zero external API costs.

Scrapers:
  - RSSFeedScraper: competitor blogs, industry publications
  - RedditScraper: subreddit posts via public JSON API
  - GoogleNewsScraper: Google News RSS for competitor/leader mentions
  - WebPageScraper: public web forums and pages
"""

import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from html import unescape

# ─── Deduplication helper ──────────────────────────────────────────

def deduplicate_items(items, existing_urls):
    """Remove items whose URL already exists in previous scrape data."""
    seen = set(existing_urls)
    deduped = []
    for item in items:
        url = item.get('url', '')
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        deduped.append(item)
    return deduped


def _is_after(date_str, since_str):
    """Check if date_str is after since_str. Returns True if can't parse (keep item)."""
    if not since_str or not date_str:
        return True
    try:
        item_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        since_dt = datetime.fromisoformat(since_str.replace('Z', '+00:00'))
        return item_dt > since_dt
    except (ValueError, TypeError):
        return True  # Keep items with unparseable dates


# ─── HTTP helper ───────────────────────────────────────────────────

USER_AGENT = 'DigitalizeMe/1.0 (content-intelligence-platform)'

def _fetch(url, timeout=30, headers=None):
    """Fetch a URL and return the response body as string."""
    hdrs = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        encoding = resp.headers.get_content_charset() or 'utf-8'
        return resp.read().decode(encoding, errors='replace')


def _fetch_json(url, timeout=30, headers=None):
    """Fetch a URL and parse as JSON."""
    return json.loads(_fetch(url, timeout=timeout, headers=headers))


# ─── RSS Feed Scraper ──────────────────────────────────────────────

class RSSFeedScraper:
    """Scrapes RSS/Atom feeds from competitor blogs and industry publications."""

    # Pre-seeded industry news feeds
    INDUSTRY_FEEDS = [
        {'name': 'Restaurant Dive', 'url': 'https://www.restaurantdive.com/feeds/news/'},
        {'name': 'Nation\'s Restaurant News', 'url': 'https://www.nrn.com/rss.xml'},
        {'name': 'Modern Restaurant Management', 'url': 'https://modernrestaurantmanagement.com/feed/'},
        {'name': 'Restaurant Technology News', 'url': 'https://restauranttechnologynews.com/feed/'},
        {'name': 'QSR Magazine', 'url': 'https://www.qsrmagazine.com/rss.xml'},
        {'name': 'Hospitality Technology', 'url': 'https://hospitalitytech.com/rss.xml'},
        {'name': 'Skift Restaurant', 'url': 'https://restaurant.skift.com/feed/'},
    ]

    def scrape_feed(self, feed_url, source_name='', max_items=25, since=None):
        """Parse an RSS/Atom feed and return structured items.
        since: ISO datetime string — only return items published after this time.
        """
        import feedparser
        try:
            feed = feedparser.parse(feed_url, agent=USER_AGENT)
            items = []
            for entry in feed.entries[:max_items]:
                pub_date = ''
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:6]).isoformat()
                    except Exception:
                        pub_date = getattr(entry, 'published', '')

                # Skip items older than since cutoff
                if since and not _is_after(pub_date, since):
                    continue

                summary = getattr(entry, 'summary', '')
                if summary:
                    # Strip HTML tags from summary
                    from bs4 import BeautifulSoup
                    summary = BeautifulSoup(summary, 'html.parser').get_text()[:500]

                items.append({
                    'title': getattr(entry, 'title', 'Untitled'),
                    'url': getattr(entry, 'link', ''),
                    'description': summary,
                    'source': source_name or feed.feed.get('title', feed_url),
                    'published_at': pub_date,
                    'type': 'rss_article'
                })
            return items
        except Exception as e:
            return [{'title': f'Feed error: {feed_url}', 'error': str(e), 'source': source_name}]

    def discover_feed(self, website_url):
        """Try to autodiscover RSS feed URL from a website."""
        common_paths = ['/feed', '/rss', '/blog/feed', '/feed.xml', '/rss.xml',
                        '/blog/rss', '/blog/feed.xml', '/atom.xml', '/feeds/posts/default']
        base = website_url.rstrip('/')

        # Try common paths first
        for path in common_paths:
            try:
                url = base + path
                resp = _fetch(url, timeout=10)
                if '<?xml' in resp[:200] or '<rss' in resp[:500] or '<feed' in resp[:500]:
                    return url
            except Exception:
                continue

        # Try HTML autodiscovery
        try:
            from bs4 import BeautifulSoup
            html = _fetch(base, timeout=10)
            soup = BeautifulSoup(html, 'html.parser')
            link = soup.find('link', attrs={'type': ['application/rss+xml', 'application/atom+xml']})
            if link and link.get('href'):
                href = link['href']
                if href.startswith('/'):
                    href = base + href
                return href
        except Exception:
            pass

        return None

    def scrape_industry_news(self, max_per_feed=10, since=None):
        """Scrape all pre-seeded industry news feeds."""
        all_items = []
        for feed_info in self.INDUSTRY_FEEDS:
            items = self.scrape_feed(feed_info['url'], source_name=feed_info['name'],
                                     max_items=max_per_feed, since=since)
            # Filter out error items
            all_items.extend([i for i in items if not i.get('error')])
            time.sleep(0.5)  # Be polite
        return all_items

    def scrape_competitor_blogs(self, competitors, max_per_competitor=10, since=None):
        """Scrape RSS feeds for a list of competitors.
        Each competitor dict should have: name, rss_feed_url (optional), blog_url (optional)
        """
        all_items = []
        for comp in competitors:
            feed_url = comp.get('rss_feed_url', '')
            if not feed_url and comp.get('blog_url'):
                feed_url = self.discover_feed(comp['blog_url'])
            if not feed_url and comp.get('website_url'):
                feed_url = self.discover_feed(comp['website_url'])

            if feed_url:
                items = self.scrape_feed(feed_url, source_name=comp.get('name', comp.get('competitor_name', '')),
                                          max_items=max_per_competitor, since=since)
                all_items.extend([i for i in items if not i.get('error')])
            time.sleep(0.5)
        return all_items


# ─── Reddit Scraper ────────────────────────────────────────────────

class RedditScraper:
    """Scrapes Reddit content. Reddit blocks direct JSON API now,
    so we use Google News RSS to find Reddit discussions as fallback."""

    def scrape_subreddit(self, subreddit, sort='hot', limit=25, since=None):
        """Try Reddit JSON API first, fall back to Google News RSS for Reddit content."""
        subreddit = subreddit.strip('/').split('/')[-1]
        if subreddit.startswith('r/'):
            subreddit = subreddit[2:]

        # Try direct JSON API first
        url = f'https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1'
        try:
            data = _fetch_json(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            })
            items = []
            for child in data.get('data', {}).get('children', []):
                post = child.get('data', {})
                if post.get('stickied'):
                    continue
                created = ''
                if post.get('created_utc'):
                    try:
                        created = datetime.utcfromtimestamp(post['created_utc']).isoformat()
                    except Exception:
                        pass
                item = {
                    'title': post.get('title', ''),
                    'url': f"https://reddit.com{post.get('permalink', '')}",
                    'description': (post.get('selftext', '') or '')[:500],
                    'source': f"r/{subreddit}",
                    'published_at': created,
                    'score': post.get('score', 0),
                    'num_comments': post.get('num_comments', 0),
                    'author': post.get('author', ''),
                    'type': 'reddit_post'
                }
                if since and not _is_after(created, since):
                    continue
                items.append(item)
            if items:
                return items
        except Exception:
            pass  # Fall through to Google News fallback

        # Fallback: Use Google News RSS to find Reddit discussions
        import feedparser
        gn_url = f'https://news.google.com/rss/search?q=site:reddit.com+r/{subreddit}&hl=en&gl=US&ceid=US:en'
        try:
            feed = feedparser.parse(gn_url, agent=USER_AGENT)
            items = []
            for entry in feed.entries[:limit]:
                pub_date = ''
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:6]).isoformat()
                    except Exception:
                        pub_date = getattr(entry, 'published', '')
                if since and not _is_after(pub_date, since):
                    continue
                items.append({
                    'title': unescape(getattr(entry, 'title', '')),
                    'url': getattr(entry, 'link', ''),
                    'description': unescape(getattr(entry, 'summary', ''))[:500],
                    'source': f"r/{subreddit}",
                    'published_at': pub_date,
                    'type': 'reddit_post'
                })
            if items:
                return items
        except Exception:
            pass

        # Last fallback: Google News for the topic (not site-restricted)
        topic_map = {
            'RestaurantTech': 'restaurant technology POS digital',
            'restaurateur': 'restaurant owner operator business',
        }
        topic = topic_map.get(subreddit, f'{subreddit} restaurant')
        import feedparser
        gn_url2 = f'https://news.google.com/rss/search?q={urllib.parse.quote_plus(topic)}&hl=en&gl=US&ceid=US:en'
        try:
            feed = feedparser.parse(gn_url2, agent=USER_AGENT)
            items = []
            for entry in feed.entries[:limit]:
                pub_date = ''
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:6]).isoformat()
                    except Exception:
                        pub_date = getattr(entry, 'published', '')
                source = ''
                if hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    source = entry.source.title
                if since and not _is_after(pub_date, since):
                    continue
                items.append({
                    'title': unescape(getattr(entry, 'title', '')),
                    'url': getattr(entry, 'link', ''),
                    'description': unescape(getattr(entry, 'summary', ''))[:500],
                    'source': source or f'r/{subreddit} (via news)',
                    'published_at': pub_date,
                    'type': 'news_article'
                })
            return items
        except Exception as e:
            return [{'title': f'Scrape error: r/{subreddit}', 'error': str(e), 'source': f'r/{subreddit}'}]

    def scrape_multiple(self, subreddits, sort='hot', limit=15, since=None):
        """Scrape multiple subreddits."""
        all_items = []
        for sub in subreddits:
            items = self.scrape_subreddit(sub, sort=sort, limit=limit, since=since)
            all_items.extend([i for i in items if not i.get('error')])
            time.sleep(1.5)
        return all_items


# ─── Google News Scraper ───────────────────────────────────────────

class GoogleNewsScraper:
    """Scrapes Google News RSS for company/leader mentions. Free, no API key."""

    def _search_news(self, query, max_items=10, since=None):
        """Search Google News RSS for a query.
        since: ISO datetime string — only return items published after this time.
        """
        import feedparser
        encoded_q = urllib.parse.quote_plus(query)
        url = f'https://news.google.com/rss/search?q={encoded_q}&hl=en&gl=US&ceid=US:en'
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            items = []
            for entry in feed.entries[:max_items]:
                pub_date = ''
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:6]).isoformat()
                    except Exception:
                        pub_date = getattr(entry, 'published', '')

                if since and not _is_after(pub_date, since):
                    continue

                source = ''
                if hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    source = entry.source.title

                items.append({
                    'title': unescape(getattr(entry, 'title', '')),
                    'url': getattr(entry, 'link', ''),
                    'description': unescape(getattr(entry, 'summary', ''))[:500],
                    'source': source or 'Google News',
                    'published_at': pub_date,
                    'type': 'news_article'
                })
            return items
        except Exception as e:
            return [{'title': f'Google News error: {query}', 'error': str(e)}]

    def scrape_company_news(self, company_name, industry_context='restaurant technology', since=None):
        """Find recent news about a company."""
        query = f'"{company_name}" {industry_context}'
        items = self._search_news(query, max_items=8, since=since)
        for item in items:
            item['company'] = company_name
        return items

    def scrape_leader_news(self, leader_name, company='', max_items=5, since=None):
        """Find recent articles/interviews featuring an industry leader."""
        query = f'"{leader_name}"'
        if company:
            query += f' "{company}"'
        query += ' restaurant technology OR F&B OR POS'
        items = self._search_news(query, max_items=max_items, since=since)
        for item in items:
            item['leader'] = leader_name
            item['company'] = company
        return items

    def scrape_all_competitors(self, competitors, max_per=5, since=None):
        """Search Google News for all competitors."""
        all_items = []
        for comp in competitors:
            name = comp.get('competitor_name', comp.get('name', ''))
            if not name:
                continue
            items = self.scrape_company_news(name, since=since)
            all_items.extend([i for i in items[:max_per] if not i.get('error')])
            time.sleep(1.5)  # Be polite to Google
        return all_items

    def scrape_all_leaders(self, leaders, max_per=3, since=None):
        """Search Google News for all tracked leaders."""
        all_items = []
        for leader in leaders:
            name = leader.get('name', '')
            company = leader.get('company', '')
            if not name:
                continue
            items = self.scrape_leader_news(name, company, max_items=max_per, since=since)
            all_items.extend([i for i in items if not i.get('error')])
            time.sleep(1.5)
        return all_items


# ─── Web Page Scraper ──────────────────────────────────────────────

class WebPageScraper:
    """Scrapes public web pages for content. For forums and articles."""

    def scrape_page(self, url, extract_links=True):
        """Scrape a web page and extract title, text, and optionally links."""
        try:
            from bs4 import BeautifulSoup
            html = _fetch(url, timeout=20)
            soup = BeautifulSoup(html, 'html.parser')

            # Remove script, style, nav, footer
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()

            title = soup.title.string.strip() if soup.title and soup.title.string else url
            text = soup.get_text(separator='\n', strip=True)[:3000]

            result = [{
                'title': title,
                'url': url,
                'description': text[:500],
                'text': text,
                'source': urllib.parse.urlparse(url).netloc,
                'type': 'web_page'
            }]

            # Extract article/post links if requested
            if extract_links:
                seen = set()
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if href.startswith('/'):
                        parsed = urllib.parse.urlparse(url)
                        href = f"{parsed.scheme}://{parsed.netloc}{href}"
                    link_text = a.get_text(strip=True)
                    if (link_text and len(link_text) > 15 and len(link_text) < 200
                            and href not in seen and href.startswith('http')):
                        seen.add(href)
                        result.append({
                            'title': link_text,
                            'url': href,
                            'source': urllib.parse.urlparse(url).netloc,
                            'type': 'web_link'
                        })
            return result[:30]  # Cap at 30 items per page
        except Exception as e:
            return [{'title': f'Scrape error: {url}', 'error': str(e), 'url': url}]

    def scrape_multiple(self, urls, extract_links=True):
        """Scrape multiple URLs."""
        all_items = []
        for url in urls:
            items = self.scrape_page(url, extract_links=extract_links)
            all_items.extend([i for i in items if not i.get('error')])
            time.sleep(1)
        return all_items


# ─── Dispatcher ────────────────────────────────────────────────────

def run_local_scrape(scrape_type, params):
    """
    Unified dispatcher for local scraping.

    scrape_type: 'competitors', 'forums', 'leaders', 'industry_news',
                 'reddit', 'rss', 'google_news', 'web'
    params: dict with type-specific parameters
        - since: ISO datetime string for delta scraping (only return newer items)
        - existing_urls: list of URLs from previous runs for deduplication

    Returns: {'source_type': str, 'source_name': str, 'items': list[dict]}
    """
    since = params.get('since')
    existing_urls = params.get('existing_urls', [])

    if scrape_type == 'competitors':
        competitors = params.get('competitors', [])
        # Two sources: RSS blogs + Google News
        rss = RSSFeedScraper()
        gn = GoogleNewsScraper()
        blog_items = rss.scrape_competitor_blogs(competitors, since=since)
        news_items = gn.scrape_all_competitors(competitors, since=since)
        all_items = deduplicate_items(blog_items + news_items, existing_urls)
        return {
            'source_type': 'competitors',
            'source_name': 'competitor_intel',
            'items': all_items
        }

    elif scrape_type == 'forums':
        forums = params.get('forums', [])
        reddit_subs = []
        web_urls = []
        for f in forums:
            platform = f.get('platform', '')
            url = f.get('url', '')
            if platform == 'reddit' or 'reddit.com' in url:
                # Extract subreddit name
                parts = url.rstrip('/').split('/')
                for i, p in enumerate(parts):
                    if p == 'r' and i + 1 < len(parts):
                        reddit_subs.append(parts[i + 1])
                        break
            elif platform in ('facebook_group', 'linkedin_group', 'slack'):
                continue  # Skip auth-gated platforms
            elif url.startswith('http'):
                web_urls.append(url)

        items = []
        if reddit_subs:
            rs = RedditScraper()
            items.extend(rs.scrape_multiple(reddit_subs, since=since))
        if web_urls:
            ws = WebPageScraper()
            items.extend(ws.scrape_multiple(web_urls, extract_links=False))

        return {
            'source_type': 'forums',
            'source_name': 'forums_and_reddit',
            'items': deduplicate_items(items, existing_urls)
        }

    elif scrape_type == 'leaders':
        leaders = params.get('leaders', [])
        gn = GoogleNewsScraper()
        items = gn.scrape_all_leaders(leaders, since=since)
        return {
            'source_type': 'leaders',
            'source_name': 'leader_news',
            'items': deduplicate_items(items, existing_urls)
        }

    elif scrape_type == 'industry_news':
        rss = RSSFeedScraper()
        items = rss.scrape_industry_news(max_per_feed=10, since=since)
        return {
            'source_type': 'industry_news',
            'source_name': 'industry_feeds',
            'items': deduplicate_items(items, existing_urls)
        }

    elif scrape_type == 'reddit':
        subreddit = params.get('subreddit', 'RestaurantTech')
        rs = RedditScraper()
        items = rs.scrape_subreddit(subreddit, limit=params.get('limit', 25), since=since)
        return {
            'source_type': 'forums',
            'source_name': f'r/{subreddit}',
            'items': deduplicate_items([i for i in items if not i.get('error')], existing_urls)
        }

    elif scrape_type == 'rss':
        feed_url = params.get('url', '')
        name = params.get('name', '')
        rss = RSSFeedScraper()
        items = rss.scrape_feed(feed_url, source_name=name, since=since)
        return {
            'source_type': 'rss',
            'source_name': name or feed_url,
            'items': deduplicate_items([i for i in items if not i.get('error')], existing_urls)
        }

    elif scrape_type == 'google_news':
        query = params.get('query', '')
        gn = GoogleNewsScraper()
        items = gn._search_news(query, since=since)
        return {
            'source_type': 'news',
            'source_name': f'google_news:{query[:50]}',
            'items': deduplicate_items([i for i in items if not i.get('error')], existing_urls)
        }

    elif scrape_type == 'web':
        url = params.get('url', '')
        ws = WebPageScraper()
        items = ws.scrape_page(url)
        return {
            'source_type': 'web',
            'source_name': urllib.parse.urlparse(url).netloc,
            'items': [i for i in items if not i.get('error')]
        }

    else:
        return {'source_type': 'unknown', 'source_name': scrape_type, 'items': []}
