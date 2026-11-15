"""Versioned taxonomy identifiers; labels are display text, not stored IDs."""
TAXONOMY = {
    'genre': {'poetry': '诗', 'ci': '词', 'qu': '曲', 'prose': '散文', 'fiction': '小说',
              'biography': '传记', 'chronicle': '编年', 'news': '新闻', 'commentary': '注疏'},
    'bibliographic_class': {'jing': '经部', 'shi': '史部', 'zi': '子部', 'ji': '集部',
                           'ji.bieji': '集部·别集', 'ji.zongji': '集部·总集'},
    'topic': {'politics': '政治', 'war': '战争', 'frontier': '边塞', 'history': '历史',
              'philosophy': '哲学', 'religion': '宗教', 'science': '科学', 'society': '社会',
              'nature': '自然', 'love': '爱情', 'travel': '旅行'},
    'language_style': {'classical': '文言', 'vernacular': '白话', 'mixed': '文白混合'},
}
TAXONOMY_VERSION = 'corpus-tags-v1'
