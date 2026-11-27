"""内置来源方案：只有数据，没有代码。新来源经人工确认后保存在批次里，由识别流程自动复用。

每项：source 来源名；root 路径正则；require/any 结构化记录必须出现的字段；plan 映射方案
（format 和 encoding 取自文件本身）；note 提示。按顺序匹配，先到先得。
"""

POETRY = dict(text_field='paragraphs|content|para', id_field='id',
              fields={'title': 'title|rhythmic|chapter', 'authors': 'author|_parent.author',
                      'work_title': '_parent._parent.title|_parent.title'})
PERIODICAL = dict(path_pattern=r'-(?P<date>(?P<year>\d{4})\.[\d.\-]+)\.txt$', title_format='{work_title} {date}')

PROFILES = [
    dict(source='mingqing', root=r'/ming-qing-wenji-corpus/', require={'id', 'collection', 'text'},
         plan=dict(text_field='text', id_field='id', constants={'unit_type': 'page'},
                   title_format='{collection} 第{page}页', accept={'category': r'[经史子集]部.*'},
                   fields={'title': 'collection', 'work_title': 'collection', 'authors': 'author',
                           'composed_label': 'dynasty', 'category': 'siku_category'})),
    dict(source='wikisource', root=r'/wikisource-zh/', require={'id', 'title', 'text'},
         plan=dict(text_field='text', id_field='id', fields={'title': 'title'})),
    dict(source='greathangpt', root=r'/greathangpt-classical-chinese/', require={'id', 'text', 'dynasty'},
         plan=dict(text_field='text', id_field='id',
                   fields={'title': 'title', 'authors': 'author', 'composed_label': 'dynasty|era',
                           'category': 'content_category'})),
    *[dict(source='chinese_poetry', root='/chinese-poetry/' + root, any={'paragraphs'},
           plan=dict(POETRY, constants={k: v for k, v in (('composed_label', era), ('work_title', work)) if v}))
      for root, era, work in ((r'全唐诗/poet\.tang\.', '唐', '全唐诗'), (r'全唐诗/poet\.song\.', '宋', '全宋诗'),
                              (r'御定全唐詩/', '唐', '御定全唐詩'), (r'宋词/ci\.song\.', '宋', '全宋词'),
                              (r'宋词/', '宋', ''), (r'元曲/', '元', ''), (r'五代诗词/huajianji/', '', '花间集'),
                              (r'五代诗词/nantang/', '', '南唐二主词'))],
    dict(source='chinese_poetry', root=r'/chinese-poetry/', any={'paragraphs', 'content', 'para'},
         plan=dict(POETRY, path_pattern=r'/chinese-poetry/(?P<work_title>诗经|楚辞|论语|幽梦影|曹操诗集|水墨唐诗)/')),
    dict(source='daizhige', root=r'/daizhigev20/',
         plan=dict(path_pattern=r'/daizhigev20/(?P<category>.+)/(?P<title>[^/]+)\.txt$')),
    dict(source='team_texts', root=r'/new-dataset/',
         plan=dict(path_pattern=r'/(?P<composed_label>[^_/]+)_(?P<category>[^_/]+)_(?P<authors>[^_/]+)_(?P<title>[^/]+)\.txt$'),
         note='文件名按“朝代_体裁_作者_书名”解析；不符合规则的文件只取文件名作标题。'),
    dict(source='gutenberg', root=r'/gutenberg-zh/text/',
         plan=dict(trim_start=r'\*\*\* ?START OF [^\n]*\*\*\*', trim_end=r'\*\*\* ?END OF ',
                   head_patterns=[r'^Title: (?P<title>.+)$', r'^Author: (?P<authors>.+)$']),
         note='标题和作者取自文件头；正文去掉 Project Gutenberg 的头尾声明。'),
    dict(source='dongfang_ocr', root=r'/ocr_sources/dongfang/',
         plan=dict(PERIODICAL, constants={'work_title': '东方杂志', 'unit_type': 'issue'}),
         note='OCR 文本含图片标签，需在清洗规则中处理。'),
    dict(source='shenbao_ocr', root=r'/ocr_sources/shenbao/',
         plan=dict(PERIODICAL, constants={'work_title': '申报', 'unit_type': 'issue'})),
    dict(source='qing_bureau_ocr', root=r'/qing_bureau_jsonl/', require={'id', 'volume', 'text', 'ocr_mean_prob'},
         plan=dict(text_field='text', id_field='id', title_format='{work_title} 第{volume}卷',
                   constants={'work_title': '清宫造办处档案', 'composed_label': '清', 'unit_type': 'volume',
                              'license': 'apache-2.0'}),
         note='open-guji/qing_bureau_of_construction 的古籍酷 OCR，经 convert.py 按卷拼成 JSONL；'
              '低置信字原样保留，置信度统计留在原始字段。'),
    dict(source='open_guji_booktext', root=r'/booktext_jsonl/', require={'id', 'work_id', 'version_key', 'text'},
         plan=dict(text_field='text', id_field='id', constants={'unit_type': 'work'},
                   fields={'title': 'title', 'authors': 'authors', 'composed_label': 'dynasty',
                           'category': 'category', 'license': 'license'}),
         note='open-guji/book-text 的 Kanripo 与整理本，经 convert.py 每个版本一部书一条；题名、作者、朝代取自 '
              'open-guji/book-index，分类为 Kanripo 分类码，许可证按版本记录（来源标“版权未知”的留空）。'),
]

SKIP = [
    (r'/chinese-classical-corpus/(punctuate|translate)\.jsonl$', '由 corpus.jsonl 派生的指令样本，不是独立正文'),
    (r'/processed-v2/', '既有处理结果的清单和报告，不含正文'),
    (r'/chinese-poetry/(rank|strains|loader|images)/|/chinese-poetry/.*(/error/|authors?\.[^/]*json$|表面结构字)',
     '作者简介、统计或辅助数据'),
    (r'/greathangpt-classical-chinese/(all|preview)\.parquet$', '与按时代分片的文件内容重复'),
    (r'/gutenberg-zh/books\.jsonl$|_download_list\.json$', '书目或下载清单，不含正文'),
    (r'/erya-dataset/', '逐句语料，已打乱且没有篇章信息；暂不入库（用户决定，2026-11-15）'),
    (r'/chinese-poetry/.*intro\.json$', '作者简介、统计或辅助数据'),
    (r'(?i)(\.(md|ya?ml|log|lock|metadata|py|sh|db|gitignore|gitattributes)|/readme[^/]*|/license[^/]*|/requirements[^/]*)$',
     '说明或辅助文件'),
]

# 规则推断用的字段名同义词；只用于给未知来源起草方案，须人工确认。
SYNONYMS = {
    'text': ('text', 'content', 'paragraphs', 'raw_text', 'body', 'para', '正文', '内容'),
    'id': ('id', '_id', 'uuid', 'doc_id'),
    'title': ('title', 'name', 'rhythmic', 'chapter', '标题', '题名'),
    'authors': ('author', 'authors', 'writer', '作者'),
    'composed_label': ('dynasty', 'era', 'period', '朝代', '时代'),
    'category': ('category', 'genre', 'content_category', 'siku_category', '体裁', '分类'),
    'work_title': ('collection', 'book', 'work', '书名'),
}
