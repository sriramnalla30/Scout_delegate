"""Talk to the orchestrator in the terminal:  python cli.py"""
import sys
import uuid

from app.orchestrator import stream_events

DIM, RESET = '\033[2m', '\033[0m'


def show(event):
    kind = event['type']
    if kind == 'tool_call':
        print(f"{DIM}  orchestrator -> {event['name']}({event['args']}){RESET}")
    elif kind == 'tool_result':
        print(f"{DIM}  orchestrator <- {event['name']}: {event['data']}{RESET}")
    elif kind == 'sub':
        detail = event.get('name') or event.get('kind')
        extra = event.get('args') or event.get('content') or event.get('data') or ''
        print(f"{DIM}      parser: {event['kind']} {detail} {extra}{RESET}")
    elif kind == 'message':
        print(f"\nScout: {event['text']}\n")
    elif kind == 'interrupt':
        print(f"\nScout asks: {event['question']}")


def main():
    thread = uuid.uuid4().hex
    print('Scout place finder. Type a request, or "quit".\n')
    while True:
        text = input('you> ').strip()
        if text.lower() in ('quit', 'exit'):
            return
        if not text:
            continue
        for event in stream_events(thread, text):
            show(event)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
