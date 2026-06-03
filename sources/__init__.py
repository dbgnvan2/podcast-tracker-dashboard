"""Source adapters: pluggable discovery + content fetch for the topic engine.

Each adapter turns a profile arm (queries + channels/sources/authors) into a list
of normalized `document` dicts that the shared analysis/digest/report layers
consume regardless of modality (video, literature, ...). See DESIGN-multisource.md.
"""
