from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.test import SimpleTestCase


class ReplayAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in ('href', 'src'):
            if name in attrs:
                self.resources.append(attrs[name])


class OfflineExampleTests(SimpleTestCase):
    def test_offline_replay_remains_available_without_a_home_shortcut(self):
        entry = 'examples/internship.html'
        self.assertNotContains(self.client.get('/'), f'href="{static(entry)}"')
        page = Path(finders.find(entry))
        assets = ReplayAssets()
        assets.feed(page.read_text(encoding='utf-8'))
        self.assertTrue(assets.resources)
        for link in assets.resources:
            with self.subTest(link=link):
                resource = urlsplit(link)
                self.assertFalse(resource.scheme or resource.netloc or resource.path.startswith('/'))
                self.assertTrue((page.parent / resource.path).is_file())
