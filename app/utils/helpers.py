import random
import string

def generate_short_code(length: int = 8):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choices(characters, k=length))

def format_bytes(size: int):
    power = 2**10
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"
