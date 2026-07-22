from src.tools.delivery import deliver_to_file, render_markdown, save_failed_digest


def deliver_markdown(digest, out_dir: str):
    return deliver_to_file(digest, out_dir)
