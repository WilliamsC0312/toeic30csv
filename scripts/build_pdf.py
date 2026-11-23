from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import groupby
from operator import itemgetter
from pathlib import Path

import pdfplumber
import reportlab
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 34
GUTTER = 23
COLUMN_WIDTH = (PAGE_WIDTH - 2 * MARGIN - GUTTER) / 2
CONTENT_TOP = PAGE_HEIGHT - 72
CONTENT_BOTTOM = 40
MAX_HEIGHT = CONTENT_TOP - CONTENT_BOTTOM

WORD_SIZE = 10.9
POS_SIZE = 8.3
MEANING_SIZE = 10.1
HEADWORD_SIZE = 8.6
DAY_HEIGHT = 22
WORD_LEADING = 12.4
MEANING_LEADING = 12.8
BASE_ROW_HEIGHT = 27.0
GROUP_TINT = HexColor('#EDF4EF')
CENTER_GAP = 8
FILL_PADDING = (GUTTER - CENTER_GAP) / 2

POS = {
    'a.': 'adj.', 'ad.': 'adv.', 'v. n.': 'v. / n.',
}
REQUIRED_FIELDS = {'英文', '中文释义', '来源主词', '学习天数'}
WORD_PATTERN = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*$")
WORD_OR_PUNCTUATION = re.compile(r'(?a:[\w\-./%]+)| +|.', re.DOTALL)
NO_LINE_START = set('，。；、：？！）】》」』〉…％”’')
NO_LINE_END = set('（【《「『〈“‘')


def register_fonts(chinese_font, chinese_bold_font):
    windows_fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    bundled_fonts = Path(reportlab.__file__).parent / 'fonts'
    default_chinese = windows_fonts / 'msyh.ttc'
    default_bold = windows_fonts / 'msyhbd.ttc'
    if chinese_font is None and not default_chinese.is_file():
        raise ValueError('找不到微软雅黑。请通过 --chinese-font 指定支持简体中文的 TrueType 字体。')
    if chinese_bold_font is None:
        chinese_bold_font = default_bold if chinese_font is None and default_bold.is_file() else chinese_font or default_chinese
    fonts = {
        'English': (windows_fonts / 'arial.ttf', bundled_fonts / 'Vera.ttf'),
        'Word': (windows_fonts / 'arialbd.ttf', bundled_fonts / 'VeraBd.ttf'),
        'Chinese': (chinese_font or default_chinese,),
        'Heading': (chinese_bold_font,),
    }
    for name, candidates in fonts.items():
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise ValueError(f'找不到字体文件 {candidates[0]}')
        pdfmetrics.registerFont(TTFont(name, str(path)))


def read_items(source_path, encoding, supplemental_pos):
    with source_path.open(encoding=encoding, newline='') as source:
        reader = csv.DictReader(source)
        missing = REQUIRED_FIELDS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'CSV 缺少列 {"、".join(sorted(missing))}')
        raw_rows = list(reader)

    groups = defaultdict(list)
    seen = set()
    for index, row in enumerate(raw_rows, 2):
        if None in row or any(row[field] is None for field in REQUIRED_FIELDS):
            raise ValueError(f'CSV 第 {index} 条记录的字段数不一致')
        english = row['英文'].strip()
        meaning = row['中文释义'].strip()
        headword = row['来源主词'].strip()
        category = (row.get('栏目') or '').strip()
        if category == '例句':
            parts = [line.strip() for line in meaning.splitlines() if line.strip()]
            if not headword or len(parts) < 2 or not parts[0].endswith(('。', '！', '？')):
                continue
            if any(part.endswith(('。', '！', '？')) for part in parts[1:]):
                continue
            english = headword
            meaning = ' '.join(parts[1:])
            order = 0
        elif not english or not meaning or (category == '实战战略' and meaning.startswith('以以下形式使用')):
            continue
        else:
            order = {'主词': 0, '派生词': 1, 'Voca Plus': 2}.get(category, 3)

        raw_pos = (row.get('词性') or '').strip()
        part_of_speech = POS.get(raw_pos, raw_pos) or supplemental_pos.get(english, '')
        meaning = ' '.join(meaning.splitlines())
        day = row['学习天数'].strip()
        rank = (row.get('Rank') or '').strip()
        if not day.isdecimal() or int(day) < 1:
            raise ValueError(f'CSV 第 {index} 条记录的学习天数无效 {day!r}')
        group_key = (int(day), headword or english)
        item_key = (group_key, english, meaning, part_of_speech)
        if item_key in seen:
            continue
        seen.add(item_key)
        groups[group_key].append({
            'english': english,
            'meaning': meaning,
            'headword': headword,
            'pos': part_of_speech,
            'day': group_key[0],
            'group': group_key,
            'rank': int(rank) if rank.isdecimal() else math.inf,
            'order': order,
        })

    if not groups:
        raise ValueError('CSV 中没有可打印的词汇或搭配')

    def group_order(entries):
        return entries[0]['day'], min(item['rank'] for item in entries)

    items = []
    for index, entries in enumerate(sorted(groups.values(), key=group_order)):
        for item in sorted(entries, key=itemgetter('order')):
            item['tinted'] = index % 2 == 0
            items.append(item)
    return items


def width(content, font, size):
    return pdfmetrics.stringWidth(content, font, size)


def wrap_english(content, max_width):
    lines = ['']
    for word in content.split():
        candidate = ' '.join(filter(None, (lines[-1], word)))
        if lines[-1] and width(candidate, 'Word', WORD_SIZE) > max_width:
            lines.append(word)
        else:
            lines[-1] = candidate
    if any(width(line, 'Word', WORD_SIZE) > max_width for line in lines):
        raise ValueError(f'英文词条或来源主词超出栏宽 {content!r}')
    return lines


def wrap_meaning(content):
    tokens = WORD_OR_PUNCTUATION.findall(content)
    lines = []
    start = 0
    while start < len(tokens):
        if tokens[start].isspace():
            start += 1
            continue
        end = start
        line_width = 0
        while end < len(tokens):
            token_width = width(tokens[end], 'Chinese', MEANING_SIZE)
            if line_width + token_width > COLUMN_WIDTH:
                break
            line_width += token_width
            end += 1
        while end > start + 1 and (
            tokens[end - 1] in NO_LINE_END
            or (end < len(tokens) and tokens[end] in NO_LINE_START)
        ):
            end -= 1
        if end == start:
            raise ValueError(f'中文释义中的字符或连续字串超出栏宽 {tokens[start]!r}')
        lines.append(''.join(tokens[start:end]).rstrip())
        start = end
    return lines


def measure(items):
    for item in items:
        headword_space = width(item['headword'], 'English', HEADWORD_SIZE) + 10
        pos_space = width(item['pos'], 'English', POS_SIZE) + 5 if item['pos'] else 0
        item['word_lines'] = wrap_english(item['english'], COLUMN_WIDTH - headword_space - pos_space)
        item['meaning_lines'] = wrap_meaning(item['meaning'])
        item['height'] = (
            BASE_ROW_HEIGHT
            + WORD_LEADING * (len(item['word_lines']) - 1)
            + MEANING_LEADING * (len(item['meaning_lines']) - 1)
        )
        if item['height'] + DAY_HEIGHT > MAX_HEIGHT:
            raise ValueError(f'单条词条超出一栏高度 {item["english"]!r}')


def split_columns(items):
    count = len(items)
    days_before = [0]
    heights_before = [0]
    for index, item in enumerate(items):
        new_day = index == 0 or items[index - 1]['day'] != item['day']
        days_before.append(days_before[-1] + int(new_day))
        heights_before.append(heights_before[-1] + item['height'])

    def height(start, end):
        days = 1 + days_before[end] - days_before[start + 1]
        return heights_before[end] - heights_before[start] + days * DAY_HEIGHT

    minimum_columns = math.ceil(height(0, count) / MAX_HEIGHT)
    first_column_count = min(minimum_columns + minimum_columns % 2, count)
    for column_count in range(first_column_count, count + 1):
        if column_count % 2 and column_count != count:
            continue
        target = (height(0, count) + (column_count - 1) * DAY_HEIGHT) / column_count

        @lru_cache(None)
        def pick(start, remaining):
            if remaining == 0:
                return (0, ()) if start == count else (math.inf, ())
            best = (math.inf, ())
            for end in range(start + 1, count - remaining + 2):
                occupied = height(start, end)
                if occupied > MAX_HEIGHT:
                    break
                if end < count and height(end, count) > (remaining - 1) * MAX_HEIGHT:
                    continue
                splits_group = (
                    end < count and items[end - 1]['group'] == items[end]['group']
                )
                group_break = 1800 if splits_group else 0
                following_penalty, following_breaks = pick(end, remaining - 1)
                penalty = following_penalty + group_break + (occupied - target) ** 2
                if penalty < best[0]:
                    best = (penalty, (end, *following_breaks))
            return best

        _, breaks = pick(0, column_count)
        if breaks:
            starts = (0, *breaks[:-1])
            columns = [items[start:end] for start, end in zip(starts, breaks)]
            if column_count % 2:
                columns.append([])
            return columns
    raise ValueError('无法分页打印词条')


def draw_text(pdf, content, x, y, font, size, gray=0.1, align='left'):
    pdf.setFont(font, size)
    pdf.setFillGray(gray)
    if align == 'right':
        pdf.drawRightString(x, y, content)
    elif align == 'center':
        pdf.drawCentredString(x, y, content)
    else:
        pdf.drawString(x, y, content)


def rule(pdf, x1, y1, x2, y2, gray, thickness):
    pdf.setStrokeGray(gray)
    pdf.setLineWidth(thickness)
    pdf.line(x1, y1, x2, y2)


def draw_item(pdf, item, x, y):
    first_baseline = y - 8.9
    for index, line in enumerate(item['word_lines']):
        draw_text(pdf, line, x, first_baseline - index * WORD_LEADING, 'Word', WORD_SIZE)
    if item['pos']:
        last_line = item['word_lines'][-1]
        draw_text(pdf, item['pos'], x + width(last_line, 'Word', WORD_SIZE) + 5,
                  first_baseline - (len(item['word_lines']) - 1) * WORD_LEADING,
                  'English', POS_SIZE, 0.29)
    draw_text(pdf, item['headword'], x + COLUMN_WIDTH, first_baseline,
              'English', HEADWORD_SIZE, 0.31, 'right')

    meaning_baseline = first_baseline - (len(item['word_lines']) - 1) * WORD_LEADING - 12.9
    for index, line in enumerate(item['meaning_lines']):
        draw_text(pdf, line, x, meaning_baseline - index * MEANING_LEADING,
                  'Chinese', MEANING_SIZE, 0.12)


def draw_column(pdf, items, x):
    y = CONTENT_TOP
    for day, day_items in groupby(items, key=itemgetter('day')):
        draw_text(pdf, f'第 {day} 天', x, y - 10.0, 'Heading', 9.7)
        rule(pdf, x, y - 15.6, x + COLUMN_WIDTH, y - 15.6, 0.62, 0.53)
        y -= DAY_HEIGHT

        for _, group in groupby(day_items, key=itemgetter('group')):
            entries = list(group)
            height = sum(item['height'] for item in entries)
            if entries[0]['tinted']:
                pdf.setFillColor(GROUP_TINT)
                pdf.rect(x - FILL_PADDING, y - height + 0.8, COLUMN_WIDTH + 2 * FILL_PADDING, height,
                         stroke=0, fill=1)

            for item in entries:
                draw_item(pdf, item, x, y)
                y -= item['height']

    assert y >= CONTENT_BOTTOM - 0.01


def write_pdf(output_path, title, columns, n_items):
    pdf = canvas.Canvas(str(output_path), pagesize=A4, pageCompression=1, invariant=1)
    pdf.setTitle(title)
    pdf.setSubject('A4 竖版双栏词汇复习表')
    pages = len(columns) // 2
    center = PAGE_WIDTH / 2
    legend = f'{n_items} 条 · 右侧小字为来源主词'
    legend_width = width(legend, 'Chinese', 9.0)
    if width(title, 'Heading', 18.0) + legend_width + 12 > PAGE_WIDTH - 2 * MARGIN:
        raise ValueError('标题超出页眉可用空间，请缩短 --title')
    for start in range(0, len(columns), 2):
        draw_text(pdf, title, MARGIN, PAGE_HEIGHT - 45.6, 'Heading', 18.0)
        draw_text(pdf, legend, PAGE_WIDTH - MARGIN,
                  PAGE_HEIGHT - 45.6, 'Chinese', 9.0, 0.29, 'right')
        rule(pdf, MARGIN, PAGE_HEIGHT - 56.7, PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 56.7, 0.15, 0.72)
        draw_column(pdf, columns[start], MARGIN)
        draw_column(pdf, columns[start + 1], MARGIN + COLUMN_WIDTH + GUTTER)
        draw_text(pdf, f'{start // 2 + 1} / {pages}', center, 22.7,
                  'English', 8.5, 0.35, 'center')
        pdf.showPage()
    pdf.save()


def verify(output_path, columns):
    def compact(content):
        return ''.join(content.split())

    with pdfplumber.open(output_path) as document:
        assert len(document.pages) == len(columns) // 2
        for page_index, page in enumerate(document.pages):
            assert abs(page.width - PAGE_WIDTH) < 0.01
            assert abs(page.height - PAGE_HEIGHT) < 0.01
            baselines = [defaultdict(list), defaultdict(list)]
            for char in page.chars:
                x, baseline = char['matrix'][4:]
                if CONTENT_BOTTOM <= baseline <= CONTENT_TOP:
                    side = int(x > PAGE_WIDTH / 2)
                    edge = MARGIN + COLUMN_WIDTH if side == 0 else PAGE_WIDTH - MARGIN
                    assert char['x1'] <= edge + 0.03
                    baselines[side][round(baseline, 2)].append((x, char['text']))

            for side, column in enumerate(columns[2 * page_index:2 * page_index + 2]):
                expected_lines = []
                day = None
                for item in column:
                    if day != item['day']:
                        day = item['day']
                        expected_lines.append(f'第 {day} 天')
                    word_lines = item['word_lines'].copy()
                    word_lines[-1] += item['pos']
                    word_lines[0] += item['headword']
                    expected_lines.extend(word_lines + item['meaning_lines'])
                actual_lines = [
                    ''.join(char for _, char in sorted(line))
                    for _, line in sorted(baselines[side].items(), reverse=True)
                ]
                assert list(map(compact, expected_lines)) == list(map(compact, actual_lines)), (page_index, side)


def main():
    parser = argparse.ArgumentParser(description='从 TOEIC 收藏词表 CSV 生成 A4 双栏打印 PDF。')
    parser.add_argument('csv', type=Path)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--title')
    parser.add_argument('--encoding', default='utf-8-sig')
    parser.add_argument('--pos-map', type=Path, help='将缺失词性补充为英文词条到词性的 JSON 对象')
    parser.add_argument('--chinese-font', type=Path)
    parser.add_argument('--chinese-bold-font', type=Path)
    args = parser.parse_args()
    try:
        if args.pdf.suffix.lower() != '.pdf' or args.csv.resolve() == args.pdf.resolve():
            raise ValueError('请使用与输入 CSV 不同的 .pdf 输出路径')
        supplemental_pos = {}
        if args.pos_map:
            supplemental_pos = json.loads(args.pos_map.read_text(encoding='utf-8-sig'))
            if not isinstance(supplemental_pos, dict) or any(
                not isinstance(value, str) or not value.strip() for value in supplemental_pos.values()
            ):
                raise ValueError('--pos-map 必须是英文词条到非空词性字符串的 JSON 对象')
        items = read_items(args.csv, args.encoding, supplemental_pos)
        register_fonts(args.chinese_font, args.chinese_bold_font)
        measure(items)
        columns = split_columns(items)
        first_day, last_day = items[0]['day'], items[-1]['day']
        days = str(first_day) if first_day == last_day else f'{first_day}-{last_day}'
        title = args.title or f'收藏词汇 · 第 {days} 天'
        args.pdf.parent.mkdir(exist_ok=True, parents=True)
        write_pdf(args.pdf, title, columns, len(items))
        verify(args.pdf, columns)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')

    missing_pos = dict.fromkeys(item['english'] for item in items
                               if not item['pos'] and WORD_PATTERN.fullmatch(item['english']))
    print(f'{len(items)} 条，{len(columns) // 2} 页 A4 竖版。')
    print('按天统计', dict(Counter(item['day'] for item in items)))
    if missing_pos:
        print('缺失词性的单词 ' + '、'.join(missing_pos), file=sys.stderr)


if __name__ == '__main__':
    main()
