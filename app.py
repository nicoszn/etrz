class VideoDownloader:
    def __init__(self):
        self.ydl_opts = {
            'quiet': True,
            'no_warnings': False, # FIX: NEVER silence warnings with auth issues
            # FIX: Remove any custom 'http_headers'. Let yt-dlp handle them.
            # FIX: Force the 'web' client to avoid bot detection on format lists.
            'extractor_args': {'youtube': {'player_client': ['web']}},
        }
        
        if COOKIES_FILE.exists():
            self.ydl_opts['cookiefile'] = str(COOKIES_FILE)
            print(f"Using cookies from: {COOKIES_FILE}")
        else:
            print(f"No cookies.txt found at {COOKIES_FILE}")

    def download_video(self, url, format_id, download_id):
        output_template = str(DOWNLOAD_DIR / f'%(title)s-{download_id[:8]}.%(ext)s')
        
        opts = {
            **self.ydl_opts,
            # FIX: Use the simple, proven format string from issue #10556
            'format': f'{format_id}+bestaudio/best',
            'outtmpl': output_template,
            'progress_hooks': [self._progress_hook(download_id)],
            'merge_output_format': 'mp4',
        }
        # ... rest of your method
