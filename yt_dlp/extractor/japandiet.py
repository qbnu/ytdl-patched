import re

from ..utils import (
    clean_html,
    float_or_none,
    int_or_none,
    smuggle_url,
    traverse_obj,
    unsmuggle_url
)
from .common import InfoExtractor


class ShugiinItvBaseIE(InfoExtractor):
    @classmethod
    def _find_rooms(cls, webpage):
        return [{
            '_type': 'url',
            'id': x.group(1),
            'title': clean_html(x.group(2)).strip(),
            'url': smuggle_url(f'https://www.shugiintv.go.jp/jp/index.php?room_id={x.group(1)}', x.groups()),
            'ie_key': ShugiinItvLiveIE.ie_key(),
        } for x in re.finditer(r'<a\s+href=".+?\?room_id=(room\d+)"\s*class="play_live".+?class="s12_14">(.+?)</td>', webpage)]

    @staticmethod
    def _parse_japanese_date(text):
        if not text:
            return None
        ERA_TABLE = {
            '明治': 1868,
            '大正': 1912,
            '昭和': 1926,
            '平成': 1989,
            '令和': 2019,
        }
        ERA_RE = '|'.join(map(re.escape, ERA_TABLE.keys()))
        mobj = re.search(rf'({ERA_RE})?(\d+)年(\d+)月(\d+)日', re.sub(r'[\s\u3000]+', '', text))
        if not mobj:
            return None
        era, year, month, day = mobj.groups()
        year, month, day = map(int, (year, month, day))
        if era:
            # example input: 令和5年3月34日
            # even though each era have their end, don't check here
            year += ERA_TABLE[era]
        return '%04d%02d%02d' % (year, month, day)

    @staticmethod
    def _parse_japanese_duration(text):
        if not text:
            return None
        mobj = re.search(r'(?:(\d+)日間?)?(?:(\d+)時間?)?(?:(\d+)分)?(?:(\d+)秒)?', re.sub(r'[\s\u3000]+', '', text))
        if not mobj:
            return None
        days, hours, mins, secs = map(int_or_none, mobj.groups())

        duration = 0
        if secs:
            duration += float(secs)
        if mins:
            duration += float(mins) * 60
        if hours:
            duration += float(hours) * 60 * 60
        if days:
            duration += float(days) * 24 * 60 * 60
        return duration


class ShugiinItvLiveIE(ShugiinItvBaseIE):
    _VALID_URL = r'https?://(?:www\.)?shugiintv\.go\.jp/(?:jp|en)(?:/index\.php)?$'
    IE_DESC = '衆議院インターネット審議中継'

    @classmethod
    def suitable(cls, url):
        return super().suitable(url) and not any(x.suitable(url) for x in (ShugiinItvLiveRoomIE, ShugiinItvVodIE))

    def _real_extract(self, url):
        self.report_warning('Listing up all running proceedings as of now. To specify one proceedings to record, use link direct from the website.')
        webpage = self._download_webpage(
            'https://www.shugiintv.go.jp/jp/index.php', None,
            encoding='euc-jp')
        return self.playlist_result(self._find_rooms(webpage))


class ShugiinItvLiveRoomIE(ShugiinItvBaseIE):
    _VALID_URL = r'https?://(?:www\.)?shugiintv\.go\.jp/(?:jp|en)/index\.php\?room_id=(?P<id>room\d+)'
    IE_DESC = '衆議院インターネット審議中継 (中継)'

    def _real_extract(self, url):
        url, smug = unsmuggle_url(url)
        if smug:
            room_id, title = smug
        else:
            room_id = self._match_id(url)
            webpage = self._download_webpage(
                'https://www.shugiintv.go.jp/jp/index.php', room_id,
                encoding='euc-jp', note='Looking up for the title')
            title = traverse_obj(self._find_rooms(webpage), (lambda k, v: v['id'] == room_id, 'title'))

        formats, subtitles = self._extract_m3u8_formats_and_subtitles(
            f'https://hlslive.shugiintv.go.jp/{room_id}/amlst:{room_id}/playlist.m3u8',
            room_id, ext='mp4')
        self._sort_formats(formats)

        return {
            'id': room_id,
            'title': title,
            'formats': formats,
            'subtitles': subtitles,
            'is_live': True,
        }


class ShugiinItvVodIE(ShugiinItvBaseIE):
    _VALID_URL = r'https?://(?:www\.)?shugiintv\.go\.jp/(?:jp|en)/index\.php\?ex=VL(?:\&[^=]+=[^&]*)*\&deli_id=(?P<id>\d+)'
    IE_DESC = '衆議院インターネット審議中継 (ビデオライブラリ)'
    _TESTS = [{
        'url': 'https://www.shugiintv.go.jp/jp/index.php?ex=VL&media_type=&deli_id=53846',
        'info_dict': {
            'id': '53846',
            'title': 'ウクライナ大統領国会演説（オンライン）',
            'release_date': '20220323',
            'chapters': 'count:4',
        }
    }, {
        'url': 'https://www.shugiintv.go.jp/en/index.php?ex=VL&media_type=&deli_id=53846',
        'only_matching': True
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(
            f'https://www.shugiintv.go.jp/jp/index.php?ex=VL&media_type=&deli_id={video_id}', video_id,
            encoding='euc-jp')

        m3u8_url = self._search_regex(
            r'id="vtag_src_base_vod"\s*value="(http.+?\.m3u8)"', webpage, 'm3u8 url')
        m3u8_url = re.sub(r'^http://', 'https://', m3u8_url)
        formats, subtitles = self._extract_m3u8_formats_and_subtitles(
            m3u8_url, video_id, ext='mp4')
        self._sort_formats(formats)

        title = self._html_search_regex(
            (r'<td\s+align="left">(.+)\s*\(\d+分\)',
             r'<TD.+?<IMG\s*src=".+?/spacer\.gif".+?height="15">(.+?)<IMG'), webpage, 'title', fatal=False)

        release_date = self._parse_japanese_date(self._html_search_regex(
            r'開会日</td>\s*<td.+?/td>\s*<TD>(.+?)</TD>',
            webpage, 'title', fatal=False))

        # NOTE: chapters are sparse, because of how the website serves the video
        chapters = []
        for chp in re.finditer(r'<A\s+HREF=".+?php\?.+?&deli_id=\d+&time=([\d\.]+)"\s*class="play_vod">(?!<img)(.+)</[Aa]>', webpage):
            chapters.append({
                'title': clean_html(chp.group(2)).strip(),
                'start_time': float_or_none(chp.group(1).strip()),
            })
        # the exact duration for the last chapter is unknown! (we can get at most minutes of granularity)
        for idx in range(len(chapters) - 1):
            chapters[idx]['end_time'] = chapters[idx + 1]['start_time']

        last_tr = re.findall(r'(?s)<TR\s*class="s14_24">(.+?)</TR>', webpage)[-1]
        if last_tr and chapters:
            last_td = re.findall(r'<TD.+?</TD>', last_tr.group(0))[-1]
            if last_td:
                chapters[-1]['end_time'] = chapters[-1]['start_time'] + self._parse_japanese_duration(clean_html(last_td.group(0)))

        return {
            'id': video_id,
            'title': title,
            'release_date': release_date,
            'chapters': chapters,
            'formats': formats,
            'subtitles': subtitles,
        }


# IDR=Internet Deliberation Relay
class SangiinIDRIE(InfoExtractor):
    def _real_extract(self, url):
        return super()._real_extract(url)
