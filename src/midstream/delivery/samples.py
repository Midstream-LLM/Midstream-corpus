"""样本切分：每个函数把一篇文档变成若干行。以后的 SFT 对话格式在这里加一项。"""

import re

SAMPLE_FIELDS = ['sample_index', 'start_char', 'end_char']
SENTENCE_END = re.compile(r'[。！？!?][」』”"]?|\.(?=\s|$)')


def _document(document, options):
    return (document,)


def _samples(document, options):
    text = document['text']
    start, index = 0, 0
    while start < len(text):
        end = min(start + options.max_chars, len(text))
        if end < len(text):
            part = text[start:end]
            boundary = part.rfind('\n\n', options.overlap_chars + 1)
            if boundary >= 0:
                end = start + boundary + 2
            else:
                boundary = part.rfind('\n', options.overlap_chars + 1)
                if boundary >= 0:
                    end = start + boundary + 1
                else:
                    for match in SENTENCE_END.finditer(part, options.overlap_chars + 1):
                        end = start + match.end()
        yield {**document, 'text': text[start:end], 'sample_index': index,
               'start_char': start, 'end_char': end}
        if end == len(text):
            break
        start = end - options.overlap_chars
        index += 1


SAMPLERS = {'document': _document, 'sample': _samples}
