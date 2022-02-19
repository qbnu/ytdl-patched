from __future__ import unicode_literals

import collections
import io
import itertools
import os
import subprocess
import time
import re
import json
import tempfile

from ..longname import split_longname_str
from .common import AudioConversionError, PostProcessor
from ._attachments import RunsFFmpeg, ShowsProgress

from ..compat import compat_str
from ..utils import (
    determine_ext,
    dfxp2srt,
    encodeArgument,
    encodeFilename,
    float_or_none,
    _get_exe_version_output,
    detect_exe_version,
    is_outdated_version,
    ISO639Utils,
    orderedSet,
    Popen,
    PostProcessingError,
    prepend_extension,
    replace_extension,
    shell_quote,
    traverse_obj,
    variadic,
    write_json_file,
)


EXT_TO_OUT_FORMATS = {
    'aac': 'adts',
    'flac': 'flac',
    'm4a': 'ipod',
    'mka': 'matroska',
    'mkv': 'matroska',
    'mpg': 'mpeg',
    'ogv': 'ogg',
    'ts': 'mpegts',
    'wma': 'asf',
    'wmv': 'asf',
    'vtt': 'webvtt',
}
ACODECS = {
    'mp3': 'libmp3lame',
    'aac': 'aac',
    'flac': 'flac',
    'm4a': 'aac',
    'opus': 'libopus',
    'vorbis': 'libvorbis',
    'wav': None,
    'alac': None,
}


class FFmpegPostProcessorError(PostProcessingError):
    def __init__(self, msg=None, retval=None):
        super().__init__(msg=msg)
        self.retval = retval


class FFmpegPostProcessor(PostProcessor, RunsFFmpeg, ShowsProgress):
    # Do NOT enable unless the PP runs ffmpeg ONLY ONCE
    # ref. https://discord.com/channels/807245652072857610/808027148308840478/920337647732404244
    #      (yt-dlp contributors only)
    _NATIVE_PROGRESS_ENABLED = False

    def __init__(self, downloader=None):
        ShowsProgress.__init__(self, downloader)
        PostProcessor.__init__(self, downloader)
        self._PROGRESS_LABEL = self.pp_key()
        self._determine_executables()

    @property
    def use_native_progress(self):
        # don't take --verbose in account since PPs don't redirect ffmpeg output to respective stdfds
        return self._NATIVE_PROGRESS_ENABLED and self._downloader and self._downloader.params.get('enable_ffmpeg_native_progress')

    def set_downloader(self, downloader):
        PostProcessor.set_downloader(self, downloader)
        if self.use_native_progress:
            self._enable_progress(False)

    def check_version(self):
        if not self.available:
            raise FFmpegPostProcessorError('ffmpeg not found. Please install or provide the path using --ffmpeg-location')

        required_version = '10-0' if self.basename == 'avconv' else '1.0'
        if is_outdated_version(
                self._versions[self.basename], required_version):
            warning = 'Your copy of %s is outdated, update %s to version %s or newer if you encounter any errors.' % (
                self.basename, self.basename, required_version)
            self.report_warning(warning)

    @staticmethod
    def get_versions_and_features(downloader=None):
        pp = FFmpegPostProcessor(downloader)
        return pp._versions, pp._features

    @staticmethod
    def get_versions(downloader=None):
        return FFmpegPostProcessor.get_version_and_features(downloader)[0]

    def _determine_executables(self):
        programs = ['avprobe', 'avconv', 'ffmpeg', 'ffprobe']

        def get_ffmpeg_version(path, prog):
            out = _get_exe_version_output(path, ['-bsfs'])
            ver = detect_exe_version(out) if out else False
            if ver:
                regexs = [
                    r'(?:\d+:)?([0-9.]+)-[0-9]+ubuntu[0-9.]+$',  # Ubuntu, see [1]
                    r'n([0-9.]+)$',  # Arch Linux
                    # 1. http://www.ducea.com/2006/06/17/ubuntu-package-version-naming-explanation/
                ]
                for regex in regexs:
                    mobj = re.match(regex, ver)
                    if mobj:
                        ver = mobj.group(1)
            self._versions[prog] = ver
            if prog != 'ffmpeg' or not out:
                return

            mobj = re.search(r'(?m)^\s+libavformat\s+(?:[0-9. ]+)\s+/\s+(?P<runtime>[0-9. ]+)', out)
            lavf_runtime_version = mobj.group('runtime').replace(' ', '') if mobj else None
            self._features = {
                'fdk': '--enable-libfdk-aac' in out,
                'setts': 'setts' in out.splitlines(),
                'needs_adtstoasc': is_outdated_version(lavf_runtime_version, '57.56.100', False),
            }

        self.basename = None
        self.probe_basename = None
        self._paths = None
        self._versions = None
        self._features = {}

        prefer_ffmpeg = self.get_param('prefer_ffmpeg', True)
        location = self.get_param('ffmpeg_location')
        if location is None:
            self._paths = {p: p for p in programs}
        else:
            if not os.path.exists(location):
                self.report_warning(
                    'ffmpeg-location %s does not exist! '
                    'Continuing without ffmpeg.' % (location))
                self._versions = {}
                return
            elif os.path.isdir(location):
                dirname, basename = location, None
            else:
                basename = os.path.splitext(os.path.basename(location))[0]
                basename = next((p for p in programs if basename.startswith(p)), 'ffmpeg')
                dirname = os.path.dirname(os.path.abspath(location))
                if basename in ('ffmpeg', 'ffprobe'):
                    prefer_ffmpeg = True

            self._paths = dict(
                (p, os.path.join(dirname, p)) for p in programs)
            if basename:
                self._paths[basename] = location

        self._versions = {}
        for p in programs:
            get_ffmpeg_version(self._paths[p], p)

        if prefer_ffmpeg is False:
            prefs = ('avconv', 'ffmpeg')
        else:
            prefs = ('ffmpeg', 'avconv')
        for p in prefs:
            if self._versions[p]:
                self.basename = p
                break

        if prefer_ffmpeg is False:
            prefs = ('avprobe', 'ffprobe')
        else:
            prefs = ('ffprobe', 'avprobe')
        for p in prefs:
            if self._versions[p]:
                self.probe_basename = p
                break

        if self.basename == 'avconv':
            self.deprecation_warning(
                'Support for avconv is deprecated and may be removed in a future version. Use ffmpeg instead')
        if self.probe_basename == 'avprobe':
            self.deprecation_warning(
                'Support for avprobe is deprecated and may be removed in a future version. Use ffprobe instead')

    @property
    def available(self):
        return self.basename is not None

    @property
    def executable(self):
        return self._paths[self.basename]

    @property
    def probe_available(self):
        return self.probe_basename is not None

    @property
    def probe_executable(self):
        return self._paths[self.probe_basename]

    @staticmethod
    def stream_copy_opts(copy=True, *, ext=None):
        yield from ('-map', '0')
        # Don't copy Apple TV chapters track, bin_data
        # See https://github.com/yt-dlp/yt-dlp/issues/2, #19042, #19024, https://trac.ffmpeg.org/ticket/6016
        yield from ('-dn', '-ignore_unknown')
        if copy:
            yield from ('-c', 'copy')
        # For some reason, '-c copy -map 0' is not enough to copy subtitles
        if ext in ('mp4', 'mov'):
            yield from ('-c:s', 'mov_text')

    def get_audio_codec(self, path):
        if not self.probe_available and not self.available:
            raise PostProcessingError('ffprobe and ffmpeg not found. Please install or provide the path using --ffmpeg-location')
        try:
            if self.probe_available:
                cmd = [
                    encodeFilename(self.probe_executable, True),
                    encodeArgument('-show_streams')]
            else:
                cmd = [
                    encodeFilename(self.executable, True),
                    encodeArgument('-i')]
            cmd.append(self._ffmpeg_fn_arg_split(path, True, True))
            self.write_debug('%s command line: %s' % (self.basename, shell_quote(cmd)))
            handle = Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout_data, stderr_data = handle.communicate_or_kill()
            expected_ret = 0 if self.probe_available else 1
            if handle.wait() != expected_ret:
                return None
        except (IOError, OSError):
            return None
        output = (stdout_data if self.probe_available else stderr_data).decode('ascii', 'ignore')
        if self.probe_available:
            audio_codec = None
            for line in output.split('\n'):
                if line.startswith('codec_name='):
                    audio_codec = line.split('=')[1].strip()
                elif line.strip() == 'codec_type=audio' and audio_codec is not None:
                    return audio_codec
        else:
            # Stream #FILE_INDEX:STREAM_INDEX[STREAM_ID](LANGUAGE): CODEC_TYPE: CODEC_NAME
            mobj = re.search(
                r'Stream\s*#\d+:\d+(?:\[0x[0-9a-f]+\])?(?:\([a-z]{3}\))?:\s*Audio:\s*([0-9a-z]+)',
                output)
            if mobj:
                return mobj.group(1)
        return None

    def get_metadata_object(self, path, opts=[]):
        if self.probe_basename != 'ffprobe':
            if self.probe_available:
                self.report_warning('Only ffprobe is supported for metadata extraction')
            raise PostProcessingError('ffprobe not found. Please install or provide the path using --ffmpeg-location')
        self.check_version()

        cmd = [
            encodeFilename(self.probe_executable, True),
            encodeArgument('-hide_banner'),
            encodeArgument('-show_format'),
            encodeArgument('-show_streams'),
            encodeArgument('-print_format'),
            encodeArgument('json'),
        ]

        cmd += opts
        cmd.append(self._ffmpeg_fn_arg_split(path, True, True))
        self.write_debug('ffprobe command line: %s' % shell_quote(cmd))
        p = Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
        stdout, stderr = p.communicate()
        return json.loads(stdout.decode('utf-8', 'replace'))

    def get_stream_number(self, path, keys, value):
        streams = self.get_metadata_object(path)['streams']
        num = next(
            (i for i, stream in enumerate(streams) if traverse_obj(stream, keys, casesense=False) == value),
            None)
        return num, len(streams)

    def _get_real_video_duration(self, filepath, fatal=True):
        try:
            duration = float_or_none(
                traverse_obj(self.get_metadata_object(filepath), ('format', 'duration')))
            if not duration:
                raise PostProcessingError('ffprobe returned empty duration')
            return duration
        except PostProcessingError as e:
            if fatal:
                raise PostProcessingError(f'Unable to determine video duration: {e.msg}')

    def _duration_mismatch(self, d1, d2):
        if not d1 or not d2:
            return None
        # The duration is often only known to nearest second. So there can be <1sec disparity natually.
        # Further excuse an additional <1sec difference.
        return abs(d1 - d2) > 2

    def run_ffmpeg_multiple_files(self, input_paths, out_path, opts, **kwargs):
        return self.real_run_ffmpeg(
            [(path, []) for path in input_paths],
            [(out_path, opts)], **kwargs)

    def real_run_ffmpeg(self, input_path_opts, output_path_opts, *, expected_retcodes=(0,), info_dict=None):
        self.check_version()

        oldest_mtime = min(
            self._downloader.stat(path).st_mtime for path, _ in input_path_opts if path)

        cmd = [encodeFilename(self.executable, True), encodeArgument('-y')]

        use_native_progress = self.use_native_progress
        if use_native_progress:
            cmd.extend(['-progress', 'pipe:1'])
        # avconv does not have repeat option
        if self.basename == 'ffmpeg':
            cmd += [encodeArgument('-loglevel'), encodeArgument('repeat+info')]

        def make_args(file, args, name, number):
            keys = ['_%s%d' % (name, number), '_%s' % name]
            if name == 'o':
                args += ['-movflags', '+faststart']
                if number == 1:
                    keys.append('')
            args += self._configuration_args(self.basename, keys)
            if name == 'i':
                args.append('-i')
            return (
                [encodeArgument(arg) for arg in args]
                + [self._ffmpeg_fn_arg_split(file, True, True)])

        for arg_type, path_opts in (('i', input_path_opts), ('o', output_path_opts)):
            cmd += itertools.chain.from_iterable(
                make_args(path, list(opts), arg_type, i + 1)
                for i, (path, opts) in enumerate(path_opts) if path)

        self.write_debug('ffmpeg command line: %s' % shell_quote(cmd))
        if use_native_progress:
            # this is required because read_ffmpeg_status doesn't care about stderr,
            # and sabotaging reading stderr cause ffmpeg to stuck
            with tempfile.TemporaryFile() as ste:
                p = Popen(cmd, stdout=subprocess.PIPE, stderr=ste, stdin=subprocess.PIPE)
                try:
                    retval = -1
                    retval = self.read_ffmpeg_status(info_dict, p, True)
                finally:
                    self._finish_multiline_status()
                ste.seek(0)
                stderr = ste.read()
        else:
            p = Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
            stderr = p.communicate_or_kill()[1]
            retval = p.returncode

        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', 'replace')
        if retval not in variadic(expected_retcodes):
            stderr = stderr.strip()
            self.write_debug(stderr)
            raise FFmpegPostProcessorError(stderr.split('\n')[-1], retval)

        for out_path, _ in output_path_opts:
            if out_path:
                self.try_utime(out_path, oldest_mtime, oldest_mtime)
        return stderr

    def run_ffmpeg(self, path, out_path, opts, **kwargs):
        return self.run_ffmpeg_multiple_files([path], out_path, opts, **kwargs)

    @staticmethod
    def _ffmpeg_filename_argument(fn):
        # Always use 'file:' because the filename may contain ':' (ffmpeg
        # interprets that as a protocol) or can start with '-' (-- is broken in
        # ffmpeg, see https://ffmpeg.org/trac/ffmpeg/ticket/2127 for details)
        # Also leave '-' intact in order not to break streaming to stdout.
        if fn.startswith(('http://', 'https://')):
            return fn
        return 'file:' + fn if fn != '-' else fn

    def _ffmpeg_fn_arg_split(self, fn, encoded=False, for_subproc=False):
        " Same as self.encode_filename_fixed(self._ffmpeg_filename_argument(path), True) but with split_longname "

        def may_encode(text):
            if encoded:
                return encodeFilename(text, for_subproc)
            else:
                return text

        if fn.startswith(('http://', 'https://')):
            return may_encode(fn)

        return may_encode('file:' + split_longname_str(fn) if fn != '-' else fn)

    @staticmethod
    def _quote_for_ffmpeg(string):
        # See https://ffmpeg.org/ffmpeg-utils.html#toc-Quoting-and-escaping
        # A sequence of '' produces '\'''\'';
        # final replace removes the empty '' between \' \'.
        string = string.replace("'", r"'\''").replace("'''", "'")
        # Handle potential ' at string boundaries.
        string = string[1:] if string[0] == "'" else "'" + string
        return string[:-1] if string[-1] == "'" else string + "'"

    def force_keyframes(self, filename, timestamps):
        timestamps = orderedSet(timestamps)
        if timestamps[0] == 0:
            timestamps = timestamps[1:]
        keyframe_file = prepend_extension(filename, 'keyframes.temp')
        self.to_screen(f'Re-encoding "{filename}" with appropriate keyframes')
        self.run_ffmpeg(filename, keyframe_file, [
            *self.stream_copy_opts(False, ext=determine_ext(filename)),
            '-force_key_frames', ','.join(f'{t:.6f}' for t in timestamps)])
        return keyframe_file

    def concat_files(self, in_files, out_file, concat_opts=None):
        """
        Use concat demuxer to concatenate multiple files having identical streams.

        Only inpoint, outpoint, and duration concat options are supported.
        See https://ffmpeg.org/ffmpeg-formats.html#concat-1 for details
        """
        concat_file = f'{out_file}.concat'
        self.write_debug(f'Writing concat spec to {concat_file}')
        with open(concat_file, 'wt', encoding='utf-8') as f:
            f.writelines(self._concat_spec(in_files, concat_opts))

        out_flags = list(self.stream_copy_opts(ext=determine_ext(out_file)))

        self.real_run_ffmpeg(
            [(concat_file, ['-hide_banner', '-nostdin', '-f', 'concat', '-safe', '0'])],
            [(out_file, out_flags)])
        os.remove(concat_file)

    @classmethod
    def _concat_spec(cls, in_files, concat_opts=None):
        if concat_opts is None:
            concat_opts = [{}] * len(in_files)
        yield 'ffconcat version 1.0\n'
        for file, opts in zip(in_files, concat_opts):
            yield f'file {cls._quote_for_ffmpeg(cls._ffmpeg_filename_argument(file))}\n'
            # Iterate explicitly to yield the following directives in order, ignoring the rest.
            for directive in 'inpoint', 'outpoint', 'duration':
                if directive in opts:
                    yield f'{directive} {opts[directive]}\n'

    def report_progress(self, s):
        if self.use_native_progress:
            if s.get('status') == 'finished' and not s.get('__from_ffmpeg_native_status'):
                return
            ShowsProgress.report_progress(self, s)
        PostProcessor.report_progress(self, s)


class FFmpegExtractAudioPP(FFmpegPostProcessor):
    COMMON_AUDIO_EXTS = ('wav', 'flac', 'm4a', 'aiff', 'mp3', 'ogg', 'mka', 'opus', 'wma')
    SUPPORTED_EXTS = ('best', 'aac', 'flac', 'mp3', 'm4a', 'opus', 'vorbis', 'wav', 'alac')

    def __init__(self, downloader=None, preferredcodec=None, preferredquality=None, nopostoverwrites=False):
        FFmpegPostProcessor.__init__(self, downloader)
        self._preferredcodec = preferredcodec or 'best'
        self._preferredquality = float_or_none(preferredquality)
        self._nopostoverwrites = nopostoverwrites

    def _quality_args(self, codec):
        if self._preferredquality is None:
            return []
        elif self._preferredquality > 10:
            return ['-b:a', f'{self._preferredquality}k']

        limits = {
            'libmp3lame': (10, 0),
            'libvorbis': (0, 10),
            # FFmpeg's AAC encoder does not have an upper limit for the value of -q:a.
            # Experimentally, with values over 4, bitrate changes were minimal or non-existent
            'aac': (0.1, 4),
            'libfdk_aac': (1, 5),
        }.get(codec)
        if not limits:
            return []

        q = limits[1] + (limits[0] - limits[1]) * (self._preferredquality / 10)
        if codec == 'libfdk_aac':
            return ['-vbr', f'{int(q)}']
        return ['-q:a', f'{q}']

    def run_ffmpeg(self, path, out_path, codec, more_opts, information=None):
        if codec is None:
            acodec_opts = []
        else:
            acodec_opts = ['-acodec', codec]
        opts = ['-vn'] + acodec_opts + more_opts
        try:
            FFmpegPostProcessor.run_ffmpeg(self, path, out_path, opts, info_dict=information)
        except FFmpegPostProcessorError as err:
            raise AudioConversionError(err.msg)

    @PostProcessor._restrict_to(images=False)
    def run(self, information):
        orig_path = path = information['filepath']
        orig_ext = information['ext']

        if self._preferredcodec == 'best' and orig_ext in self.COMMON_AUDIO_EXTS:
            self.to_screen('Skipping audio extraction since the file is already in a common audio format')
            return [], information

        filecodec = self.get_audio_codec(path)
        if filecodec is None:
            raise PostProcessingError('WARNING: unable to obtain file audio codec with ffprobe')

        more_opts = []
        if self._preferredcodec == 'best' or self._preferredcodec == filecodec or (self._preferredcodec == 'm4a' and filecodec == 'aac'):
            if filecodec == 'aac' and self._preferredcodec in ['m4a', 'best']:
                # Lossless, but in another container
                acodec = 'copy'
                extension = 'm4a'
                more_opts = ['-bsf:a', 'aac_adtstoasc']
            elif filecodec in ['aac', 'flac', 'mp3', 'vorbis', 'opus']:
                # Lossless if possible
                acodec = 'copy'
                extension = filecodec
                if filecodec == 'aac':
                    more_opts = ['-f', 'adts']
                if filecodec == 'vorbis':
                    extension = 'ogg'
            elif filecodec == 'alac':
                acodec = None
                extension = 'm4a'
                more_opts += ['-acodec', 'alac']
            else:
                # MP3 otherwise.
                acodec = 'libmp3lame'
                extension = 'mp3'
                more_opts = self._quality_args(acodec)
        else:
            # We convert the audio (lossy if codec is lossy)
            acodec = ACODECS[self._preferredcodec]
            if acodec == 'aac' and self._features.get('fdk'):
                acodec = 'libfdk_aac'
            extension = self._preferredcodec
            more_opts = self._quality_args(acodec)
            if self._preferredcodec == 'aac':
                more_opts += ['-f', 'adts']
            elif self._preferredcodec == 'm4a':
                more_opts += ['-bsf:a', 'aac_adtstoasc']
            elif self._preferredcodec == 'vorbis':
                extension = 'ogg'
            elif self._preferredcodec == 'wav':
                extension = 'wav'
                more_opts += ['-f', 'wav']
            elif self._preferredcodec == 'alac':
                extension = 'm4a'
                more_opts += ['-acodec', 'alac']

        prefix, sep, ext = path.rpartition('.')  # not os.path.splitext, since the latter does not work on unicode in all setups
        temp_path = new_path = prefix + sep + extension

        if new_path == path:
            orig_path = prepend_extension(path, 'orig')
            temp_path = prepend_extension(path, 'temp')
        if (self._nopostoverwrites and self._downloader.exists(encodeFilename(new_path))
                and self._downloader.exists(encodeFilename(orig_path))):
            self.to_screen('Post-process file %s exists, skipping' % new_path)
            return [], information

        try:
            self.to_screen(f'Destination: {new_path}')
            self.run_ffmpeg(path, temp_path, acodec, more_opts, information)
        except AudioConversionError as e:
            raise PostProcessingError(
                'audio conversion failed: ' + e.msg)
        except Exception:
            raise PostProcessingError('error running ' + self.basename)

        os.replace(path, orig_path)
        os.replace(temp_path, new_path)
        information['filepath'] = new_path
        information['ext'] = extension

        # Try to update the date time for extracted audio file.
        if information.get('filetime') is not None:
            self.try_utime(
                new_path, time.time(), information['filetime'],
                errnote='Cannot update utime of audio file')

        return [orig_path], information


class FFmpegVideoConvertorPP(FFmpegPostProcessor):
    SUPPORTED_EXTS = ('mp4', 'mkv', 'flv', 'webm', 'mov', 'avi', 'mp3', 'mka', 'm4a', 'ogg', 'opus')
    FORMAT_RE = re.compile(r'{0}(?:/{0})*$'.format(r'(?:\w+>)?(?:%s)' % '|'.join(SUPPORTED_EXTS)))
    _ACTION = 'converting'

    def __init__(self, downloader=None, preferedformat=None):
        super(FFmpegVideoConvertorPP, self).__init__(downloader)
        self._preferedformats = preferedformat.lower().split('/')

    def _target_ext(self, source_ext):
        for pair in self._preferedformats:
            kv = pair.split('>')
            if len(kv) == 1 or kv[0].strip() == source_ext:
                return kv[-1].strip()

    @staticmethod
    def _options(target_ext):
        if target_ext == 'avi':
            return ['-c:v', 'libxvid', '-vtag', 'XVID']
        return []

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        filename, source_ext = info['filepath'], info['ext'].lower()
        target_ext = self._target_ext(source_ext)
        _skip_msg = (
            f'could not find a mapping for {source_ext}' if not target_ext
            else f'already is in target format {source_ext}' if source_ext == target_ext
            else None)
        if _skip_msg:
            self.to_screen(f'Not {self._ACTION} media file "{filename}"; {_skip_msg}')
            return [], info

        outpath = replace_extension(filename, target_ext, source_ext)
        self.to_screen(f'{self._ACTION.title()} video from {source_ext} to {target_ext}; Destination: {outpath}')
        self.run_ffmpeg(filename, outpath, self._options(target_ext))

        info['filepath'] = outpath
        info['format'] = info['ext'] = target_ext
        return [filename], info


class FFmpegVideoRemuxerPP(FFmpegVideoConvertorPP):
    _ACTION = 'remuxing'
    _NATIVE_PROGRESS_ENABLED = True

    @staticmethod
    def _options(target_ext):
        return FFmpegPostProcessor.stream_copy_opts()


class FFmpegEmbedSubtitlePP(FFmpegPostProcessor):
    _NATIVE_PROGRESS_ENABLED = True

    def __init__(self, downloader=None, already_have_subtitle=False):
        super(FFmpegEmbedSubtitlePP, self).__init__(downloader)
        self._already_have_subtitle = already_have_subtitle

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        if info['ext'] not in ('mp4', 'webm', 'mkv'):
            self.to_screen('Subtitles can only be embedded in mp4, webm or mkv files')
            return [], info
        subtitles = info.get('requested_subtitles')
        if not subtitles:
            self.to_screen('There aren\'t any subtitles to embed')
            return [], info

        filename = info['filepath']

        # Disabled temporarily. There needs to be a way to overide this
        # in case of duration actually mismatching in extractor
        # See: https://github.com/yt-dlp/yt-dlp/issues/1870, https://github.com/yt-dlp/yt-dlp/issues/1385
        '''
        if info.get('duration') and not info.get('__real_download') and self._duration_mismatch(
                self._get_real_video_duration(filename, False), info['duration']):
            self.to_screen(f'Skipping {self.pp_key()} since the real and expected durations mismatch')
            return [], info
        '''

        ext = info['ext']
        sub_langs, sub_names, sub_filenames = [], [], []
        webm_vtt_warn = False
        mp4_ass_warn = False

        for lang, sub_info in subtitles.items():
            if not os.path.exists(sub_info.get('filepath', '')):
                self.report_warning(f'Skipping embedding {lang} subtitle because the file is missing')
                continue
            sub_ext = sub_info['ext']
            if sub_ext in ('json', 'xml'):
                self.report_warning(f'{sub_ext.upper()} subtitles cannot be embedded')
            elif ext != 'webm' or ext == 'webm' and sub_ext == 'vtt':
                sub_langs.append(lang)
                sub_names.append(sub_info.get('name'))
                sub_filenames.append(sub_info['filepath'])
            else:
                if not webm_vtt_warn and ext == 'webm' and sub_ext != 'vtt':
                    webm_vtt_warn = True
                    self.report_warning('Only WebVTT subtitles can be embedded in webm files')
            if not mp4_ass_warn and ext == 'mp4' and sub_ext == 'ass':
                mp4_ass_warn = True
                self.report_warning('ASS subtitles cannot be properly embedded in mp4 files; expect issues')

        if not sub_langs:
            return [], info

        input_files = [filename] + sub_filenames

        opts = [
            *self.stream_copy_opts(ext=info['ext']),
            # Don't copy the existing subtitles, we may be running the
            # postprocessor a second time
            '-map', '-0:s',
        ]
        for i, (lang, name) in enumerate(zip(sub_langs, sub_names)):
            opts.extend(['-map', '%d:0' % (i + 1)])
            lang_code = ISO639Utils.short2long(lang) or lang
            opts.extend(['-metadata:s:s:%d' % i, 'language=%s' % lang_code])
            if name:
                opts.extend(['-metadata:s:s:%d' % i, 'handler_name=%s' % name,
                             '-metadata:s:s:%d' % i, 'title=%s' % name])

        temp_filename = prepend_extension(filename, 'temp')
        self.to_screen('Embedding subtitles in "%s"' % filename)
        self.run_ffmpeg_multiple_files(input_files, temp_filename, opts)
        self._downloader.replace(temp_filename, filename)

        files_to_delete = [] if self._already_have_subtitle else sub_filenames
        return files_to_delete, info


class FFmpegMetadataPP(FFmpegPostProcessor):
    _NATIVE_PROGRESS_ENABLED = True

    def __init__(self, downloader, add_metadata=True, add_chapters=True, add_infojson='if_exists'):
        FFmpegPostProcessor.__init__(self, downloader)
        self._add_metadata = add_metadata
        self._add_chapters = add_chapters
        self._add_infojson = add_infojson

    @staticmethod
    def _options(target_ext):
        audio_only = target_ext == 'm4a'
        yield from FFmpegPostProcessor.stream_copy_opts(not audio_only)
        if audio_only:
            yield from ('-vn', '-acodec', 'copy')

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        filename, metadata_filename = info['filepath'], None
        files_to_delete, options = [], []
        if self._add_chapters and info.get('chapters'):
            metadata_filename = replace_extension(filename, 'meta')
            options.extend(self._get_chapter_opts(info['chapters'], metadata_filename))
            files_to_delete.append(metadata_filename)
        if self._add_metadata:
            options.extend(self._get_metadata_opts(info))

        if self._add_infojson:
            if info['ext'] in ('mkv', 'mka'):
                infojson_filename = info.get('infojson_filename')
                options.extend(self._get_infojson_opts(info, infojson_filename))
                if not infojson_filename:
                    files_to_delete.append(info.get('infojson_filename'))
            elif self._add_infojson is True:
                self.to_screen('The info-json can only be attached to mkv/mka files')

        if not options:
            self.to_screen('There isn\'t any metadata to add')
            return [], info

        temp_filename = prepend_extension(filename, 'temp')
        self.to_screen('Adding metadata to "%s"' % filename)
        self.run_ffmpeg_multiple_files(
            (filename, metadata_filename), temp_filename,
            itertools.chain(self._options(info['ext']), *options))
        for file in filter(None, files_to_delete):
            os.remove(file)  # Don't obey --keep-files
        os.replace(temp_filename, filename)
        return [], info

    @staticmethod
    def _get_chapter_opts(chapters, metadata_filename):
        with io.open(metadata_filename, 'wt', encoding='utf-8') as f:
            def ffmpeg_escape(text):
                return re.sub(r'([\\=;#\n])', r'\\\1', text)

            metadata_file_content = ';FFMETADATA1\n'
            for chapter in chapters:
                metadata_file_content += '[CHAPTER]\nTIMEBASE=1/1000\n'
                metadata_file_content += 'START=%d\n' % (chapter['start_time'] * 1000)
                metadata_file_content += 'END=%d\n' % (chapter['end_time'] * 1000)
                chapter_title = chapter.get('title')
                if chapter_title:
                    metadata_file_content += 'title=%s\n' % ffmpeg_escape(chapter_title)
            f.write(metadata_file_content)
        yield ('-map_metadata', '1')

    def _get_metadata_opts(self, info):
        meta_prefix = 'meta'
        metadata = collections.defaultdict(dict)

        def add(meta_list, info_list=None):
            value = next((
                str(info[key]) for key in [f'{meta_prefix}_'] + list(variadic(info_list or meta_list))
                if info.get(key) is not None), None)
            if value not in ('', None):
                metadata['common'].update({meta_f: value for meta_f in variadic(meta_list)})

        # See [1-4] for some info on media metadata/metadata supported
        # by ffmpeg.
        # 1. https://kdenlive.org/en/project/adding-meta-data-to-mp4-video/
        # 2. https://wiki.multimedia.cx/index.php/FFmpeg_Metadata
        # 3. https://kodi.wiki/view/Video_file_tagging

        add('title', ('track', 'title'))
        add('date', 'upload_date')
        add(('description', 'synopsis'), 'description')
        add(('purl', 'comment'), 'webpage_url')
        add('track', 'track_number')
        add('artist', ('artist', 'creator', 'uploader', 'uploader_id'))
        add('genre')
        add('album')
        add('album_artist')
        add('disc', 'disc_number')
        add('show', 'series')
        add('season_number')
        add('episode_id', ('episode', 'episode_id'))
        add('episode_sort', 'episode_number')
        if 'embed-metadata' in self.get_param('compat_opts', []):
            add('comment', 'description')
            metadata['common'].pop('synopsis', None)

        meta_regex = rf'{re.escape(meta_prefix)}(?P<i>\d+)?_(?P<key>.+)'
        for key, value in info.items():
            mobj = re.fullmatch(meta_regex, key)
            if value is not None and mobj:
                metadata[mobj.group('i') or 'common'][mobj.group('key')] = value

        for name, value in metadata['common'].items():
            yield ('-metadata', f'{name}={value}')

        stream_idx = 0
        for fmt in info.get('requested_formats') or []:
            stream_count = 2 if 'none' not in (fmt.get('vcodec'), fmt.get('acodec')) else 1
            lang = ISO639Utils.short2long(fmt.get('language') or '') or fmt.get('language')
            for i in range(stream_idx, stream_idx + stream_count):
                if lang:
                    metadata[str(i)].setdefault('language', lang)
                for name, value in metadata[str(i)].items():
                    yield (f'-metadata:s:{i}', f'{name}={value}')
            stream_idx += stream_count

    def _get_infojson_opts(self, info, infofn):
        if not infofn or not os.path.exists(infofn):
            if self._add_infojson is not True:
                return
            infofn = infofn or '%s.temp' % (
                self._downloader.prepare_filename(info, 'infojson')
                or replace_extension(self._downloader.prepare_filename(info), 'info.json', info['ext']))
            if not self._downloader._ensure_dir_exists(infofn):
                return
            self.write_debug(f'Writing info-json to: {infofn}')
            write_json_file(self._downloader.sanitize_info(info, self.get_param('clean_infojson', True)), infofn)
            info['infojson_filename'] = infofn

        old_stream, new_stream = self.get_stream_number(info['filepath'], ('tags', 'mimetype'), 'application/json')
        if old_stream is not None:
            yield ('-map', '-0:%d' % old_stream)
            new_stream -= 1

        yield ('-attach', infofn,
               '-metadata:s:%d' % new_stream, 'mimetype=application/json')


class FFmpegMergerPP(FFmpegPostProcessor):
    _NATIVE_PROGRESS_ENABLED = True

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        filename = info['filepath']
        temp_filename = prepend_extension(filename, 'temp')
        args = ['-c', 'copy']
        audio_streams = 0
        for (i, fmt) in enumerate(info['requested_formats']):
            if fmt.get('acodec') != 'none':
                args.extend(['-map', f'{i}:a:0'])
                aac_fixup = fmt['protocol'].startswith('m3u8') and self.get_audio_codec(fmt['filepath']) == 'aac'
                if aac_fixup:
                    args.extend([f'-bsf:a:{audio_streams}', 'aac_adtstoasc'])
                audio_streams += 1
            if fmt.get('vcodec') != 'none':
                args.extend(['-map', '%u:v:0' % (i)])
        self.to_screen('Merging formats into "%s"' % filename)
        self.run_ffmpeg_multiple_files(info['__files_to_merge'], temp_filename, args, info_dict=info)
        self._downloader.rename(temp_filename, filename)
        return info['__files_to_merge'], info

    def can_merge(self):
        # TODO: figure out merge-capable ffmpeg version
        if self.basename != 'avconv':
            return True

        required_version = '10-0'
        if is_outdated_version(
                self._versions[self.basename], required_version):
            warning = ('Your copy of %s is outdated and unable to properly mux separate video and audio files, '
                       'yt-dlp will download single file media. '
                       'Update %s to version %s or newer to fix this.') % (
                           self.basename, self.basename, required_version)
            self.report_warning(warning)
            return False
        return True


class FFmpegFixupPostProcessor(FFmpegPostProcessor):
    _NATIVE_PROGRESS_ENABLED = True

    def _fixup(self, msg, filename, options):
        temp_filename = prepend_extension(filename, 'temp')

        self.to_screen(f'{msg} of "{filename}"')
        self.run_ffmpeg(filename, temp_filename, options)

        self._downloader.replace(temp_filename, filename)


class FFmpegFixupStretchedPP(FFmpegFixupPostProcessor):
    @PostProcessor._restrict_to(images=False, audio=False)
    def run(self, info):
        stretched_ratio = info.get('stretched_ratio')
        if stretched_ratio not in (None, 1):
            self._fixup('Fixing aspect ratio', info['filepath'], [
                *self.stream_copy_opts(), '-aspect', '%f' % stretched_ratio])
        return [], info


class FFmpegFixupM4aPP(FFmpegFixupPostProcessor):
    @PostProcessor._restrict_to(images=False, video=False)
    def run(self, info):
        if info.get('container') == 'm4a_dash':
            self._fixup('Correcting container', info['filepath'], [*self.stream_copy_opts(), '-f', 'mp4'])
        return [], info


class FFmpegFixupM3u8PP(FFmpegFixupPostProcessor):
    def _needs_fixup(self, info):
        yield info['ext'] in ('mp4', 'm4a')
        yield info['protocol'].startswith('m3u8')
        try:
            metadata = self.get_metadata_object(info['filepath'])
        except PostProcessingError as e:
            self.report_warning(f'Unable to extract metadata: {e.msg}')
            yield True
        else:
            yield traverse_obj(metadata, ('format', 'format_name'), casesense=False) == 'mpegts'

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        if all(self._needs_fixup(info)):
            self._fixup('Fixing MPEG-TS in MP4 container', info['filepath'], [
                *self.stream_copy_opts(), '-f', 'mp4', '-bsf:a', 'aac_adtstoasc'])
        return [], info


class FFmpegFixupTimestampPP(FFmpegFixupPostProcessor):

    def __init__(self, downloader=None, trim=0.001):
        # "trim" should be used when the video contains unintended packets
        super(FFmpegFixupTimestampPP, self).__init__(downloader)
        assert isinstance(trim, (int, float))
        self.trim = str(trim)

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        if not self._features.get('setts'):
            self.report_warning(
                'A re-encode is needed to fix timestamps in older versions of ffmpeg. '
                'Please install ffmpeg 4.4 or later to fixup without re-encoding')
            opts = ['-vf', 'setpts=PTS-STARTPTS']
        else:
            opts = ['-c', 'copy', '-bsf', 'setts=ts=TS-STARTPTS']
        self._fixup('Fixing frame timestamp', info['filepath'], opts + [*self.stream_copy_opts(False), '-ss', self.trim])
        return [], info


class FFmpegCopyStreamPP(FFmpegFixupPostProcessor):
    MESSAGE = 'Copying stream'

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        self._fixup(self.MESSAGE, info['filepath'], self.stream_copy_opts())
        return [], info


class FFmpegFixupDurationPP(FFmpegCopyStreamPP):
    MESSAGE = 'Fixing video duration'


class FFmpegFixupDuplicateMoovPP(FFmpegCopyStreamPP):
    MESSAGE = 'Fixing duplicate MOOV atoms'


class FFmpegSubtitlesConvertorPP(FFmpegPostProcessor):
    SUPPORTED_EXTS = ('srt', 'vtt', 'ass', 'lrc')

    def __init__(self, downloader=None, format=None):
        super(FFmpegSubtitlesConvertorPP, self).__init__(downloader)
        self.format = format

    def run(self, info):
        subs = info.get('requested_subtitles')
        new_ext = self.format
        new_format = new_ext
        if new_format == 'vtt':
            new_format = 'webvtt'
        if subs is None:
            self.to_screen('There aren\'t any subtitles to convert')
            return [], info
        self.to_screen('Converting subtitles')
        sub_filenames = []
        for lang, sub in subs.items():
            if not os.path.exists(sub.get('filepath', '')):
                self.report_warning(f'Skipping embedding {lang} subtitle because the file is missing')
                continue
            ext = sub['ext']
            if ext == new_ext:
                self.to_screen('Subtitle file for %s is already in the requested format' % new_ext)
                continue
            elif ext == 'json':
                self.to_screen(
                    'You have requested to convert json subtitles into another format, '
                    'which is currently not possible')
                continue
            old_file = sub['filepath']
            sub_filenames.append(old_file)
            new_file = replace_extension(old_file, new_ext)

            if ext in ('dfxp', 'ttml', 'tt'):
                self.report_warning(
                    'You have requested to convert dfxp (TTML) subtitles into another format, '
                    'which results in style information loss')

                dfxp_file = old_file
                srt_file = replace_extension(old_file, 'srt')

                with open(dfxp_file, 'rb') as f:
                    srt_data = dfxp2srt(f.read())

                with io.open(srt_file, 'wt', encoding='utf-8') as f:
                    f.write(srt_data)
                old_file = srt_file

                subs[lang] = {
                    'ext': 'srt',
                    'data': srt_data,
                    'filepath': srt_file,
                }

                if new_ext == 'srt':
                    continue
                else:
                    sub_filenames.append(srt_file)

            self.run_ffmpeg(old_file, new_file, ['-f', new_format])

            with io.open(new_file, 'rt', encoding='utf-8') as f:
                subs[lang] = {
                    'ext': new_ext,
                    'data': f.read(),
                    'filepath': new_file,
                }

            info['__files_to_move'][new_file] = replace_extension(
                info['__files_to_move'][sub['filepath']], new_ext)

        return sub_filenames, info


class FFmpegSplitChaptersPP(FFmpegPostProcessor):
    def __init__(self, downloader, force_keyframes=False):
        FFmpegPostProcessor.__init__(self, downloader)
        self._force_keyframes = force_keyframes

    def _prepare_filename(self, number, chapter, info):
        info = info.copy()
        info.update({
            'section_number': number,
            'section_title': chapter.get('title'),
            'section_start': chapter.get('start_time'),
            'section_end': chapter.get('end_time'),
        })
        return self._downloader.prepare_filename(info, 'chapter')

    def _ffmpeg_args_for_chapter(self, number, chapter, info):
        destination = self._prepare_filename(number, chapter, info)
        if not self._downloader._ensure_dir_exists(encodeFilename(destination)):
            return

        chapter['filepath'] = destination
        self.to_screen('Chapter %03d; Destination: %s' % (number, destination))
        return (
            destination,
            ['-ss', compat_str(chapter['start_time']),
             '-t', compat_str(chapter['end_time'] - chapter['start_time'])])

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        chapters = info.get('chapters') or []
        if not chapters:
            self.to_screen('Chapter information is unavailable')
            return [], info

        in_file = info['filepath']
        if self._force_keyframes and len(chapters) > 1:
            in_file = self.force_keyframes(in_file, (c['start_time'] for c in chapters))
        self.to_screen('Splitting video by chapters; %d chapters found' % len(chapters))
        for idx, chapter in enumerate(chapters):
            destination, opts = self._ffmpeg_args_for_chapter(idx + 1, chapter, info)
            self.real_run_ffmpeg([(in_file, opts)], [(destination, self.stream_copy_opts())])
        if in_file != info['filepath']:
            os.remove(in_file)
        return [], info


class FFmpegThumbnailsConvertorPP(FFmpegPostProcessor):
    SUPPORTED_EXTS = ('jpg', 'png')

    def __init__(self, downloader=None, format=None):
        super(FFmpegThumbnailsConvertorPP, self).__init__(downloader)
        self.format = format

    @staticmethod
    def is_webp(path):
        with open(encodeFilename(path), 'rb') as f:
            b = f.read(12)
        return b[0:4] == b'RIFF' and b[8:] == b'WEBP'

    def fixup_webp(self, info, idx=-1):
        thumbnail_filename = info['thumbnails'][idx]['filepath']
        _, thumbnail_ext = os.path.splitext(thumbnail_filename)
        if thumbnail_ext:
            thumbnail_ext = thumbnail_ext[1:].lower()
            if thumbnail_ext != 'webp' and self.is_webp(thumbnail_filename):
                self.to_screen('Correcting thumbnail "%s" extension to webp' % thumbnail_filename)
                webp_filename = replace_extension(thumbnail_filename, 'webp')
                self._downloader.replace(thumbnail_filename, webp_filename)
                info['thumbnails'][idx]['filepath'] = webp_filename
                info['__files_to_move'][webp_filename] = replace_extension(
                    info['__files_to_move'].pop(thumbnail_filename), 'webp')

    @staticmethod
    def _options(target_ext):
        if target_ext == 'jpg':
            return ['-bsf:v', 'mjpeg2jpeg']
        return []

    def convert_thumbnail(self, thumbnail_filename, target_ext):
        thumbnail_conv_filename = replace_extension(thumbnail_filename, target_ext)

        self.to_screen('Converting thumbnail "%s" to %s' % (thumbnail_filename, target_ext))
        self.real_run_ffmpeg(
            [(thumbnail_filename, ['-f', 'image2', '-pattern_type', 'none'])],
            [(thumbnail_conv_filename.replace('%', '%%'), self._options(target_ext))])
        return thumbnail_conv_filename

    def run(self, info):
        files_to_delete = []
        has_thumbnail = False

        for idx, thumbnail_dict in enumerate(info.get('thumbnails') or []):
            original_thumbnail = thumbnail_dict.get('filepath')
            if not original_thumbnail:
                continue
            has_thumbnail = True
            self.fixup_webp(info, idx)
            _, thumbnail_ext = os.path.splitext(original_thumbnail)
            if thumbnail_ext:
                thumbnail_ext = thumbnail_ext[1:].lower()
            if thumbnail_ext == 'jpeg':
                thumbnail_ext = 'jpg'
            if thumbnail_ext == self.format:
                self.to_screen('Thumbnail "%s" is already in the requested format' % original_thumbnail)
                continue
            thumbnail_dict['filepath'] = self.convert_thumbnail(original_thumbnail, self.format)
            files_to_delete.append(original_thumbnail)
            info['__files_to_move'][thumbnail_dict['filepath']] = replace_extension(
                info['__files_to_move'][original_thumbnail], self.format)

        if not has_thumbnail:
            self.to_screen('There aren\'t any thumbnails to convert')
        return files_to_delete, info


class FFmpegConcatPP(FFmpegPostProcessor):
    def __init__(self, downloader, only_multi_video=False):
        self._only_multi_video = only_multi_video
        super().__init__(downloader)

    def concat_files(self, in_files, out_file):
        if len(in_files) == 1:
            if os.path.realpath(in_files[0]) != os.path.realpath(out_file):
                self.to_screen(f'Moving "{in_files[0]}" to "{out_file}"')
            os.replace(in_files[0], out_file)
            return []

        codecs = [traverse_obj(self.get_metadata_object(file), ('streams', ..., 'codec_name')) for file in in_files]
        if len(set(map(tuple, codecs))) > 1:
            raise PostProcessingError(
                'The files have different streams/codecs and cannot be concatenated. '
                'Either select different formats or --recode-video them to a common format')

        self.to_screen(f'Concatenating {len(in_files)} files; Destination: {out_file}')
        super().concat_files(in_files, out_file)
        return in_files

    @PostProcessor._restrict_to(images=False, simulated=False)
    def run(self, info):
        entries = info.get('entries') or []
        if not any(entries) or (self._only_multi_video and info['_type'] != 'multi_video'):
            return [], info
        elif any(len(entry) > 1 for entry in traverse_obj(entries, (..., 'requested_downloads')) or []):
            raise PostProcessingError('Concatenation is not supported when downloading multiple separate formats')

        in_files = traverse_obj(entries, (..., 'requested_downloads', 0, 'filepath')) or []
        if len(in_files) < len(entries):
            raise PostProcessingError('Aborting concatenation because some downloads failed')

        ie_copy = self._downloader._playlist_infodict(info)
        exts = traverse_obj(entries, (..., 'requested_downloads', 0, 'ext'), (..., 'ext'))
        ie_copy['ext'] = exts[0] if len(set(exts)) == 1 else 'mkv'
        out_file = self._downloader.prepare_filename(ie_copy, 'pl_video')

        files_to_delete = self.concat_files(in_files, out_file)

        info['requested_downloads'] = [{
            'filepath': out_file,
            'ext': ie_copy['ext'],
        }]
        return files_to_delete, info
