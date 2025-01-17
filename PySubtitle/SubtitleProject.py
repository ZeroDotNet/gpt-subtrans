import json
import os
import logging
import threading
from PySubtitle.Helpers import GetOutputPath
from PySubtitle.Options import Options
from PySubtitle.SubtitleError import TranslationAbortedError
from PySubtitle.SubtitleFile import SubtitleFile

from PySubtitle.SubtitleSerialisation import SubtitleDecoder, SubtitleEncoder
from PySubtitle.SubtitleTranslator import SubtitleTranslator
from PySubtitle.TranslationEvents import TranslationEvents

default_encoding = os.getenv('DEFAULT_ENCODING', 'utf-8')

class SubtitleProject:
    def __init__(self, options : Options):
        self.subtitles : SubtitleFile = None
        self.events = TranslationEvents()
        self.projectfile = None
        self.needsupdate = False
        self.lock = threading.Lock()
        self.stop_event = None

        self.include_original = options.get('include_original', False)
        
        project_mode = options.get('project', '')
        if project_mode:
            project_mode = project_mode.lower()

        self.read_project = project_mode in ["true", "read", "resume", "retranslate", "reparse"]
        self.write_project = project_mode in ["true", "write", "preview", "resume", "retranslate", "reparse"]
        self.update_project = self.write_project and not project_mode in ['reparse']
        self.load_subtitles = project_mode is None or project_mode in ["true", "write", "reload", "preview"]

        self.preview = project_mode in ["preview"]
        self.resume = project_mode in ["resume"]
        self.reparse = project_mode in ["reparse"]
        self.retranslate = project_mode in ["retranslate"]

        if self.update_project and options.get('autosave'):
            self._start_autosave_thread

    def __del__(self):
        if self.stop_event:
            self.stop_event.set()
            self.periodic_update_thread.join()

    @property
    def target_language(self):
        return self.subtitles.target_language if self.subtitles else None

    def InitialiseProject(self, filepath, outputpath = None, write_backup = False):
        """
        Initialize the project by either loading an existing project file or creating a new one.
        Load the subtitles to be translated, either from the project file or the source file.

        :param filepath: the path to the project or a source subtitle file (in .srt format) to be translated
        :param outputpath: the path to write the translated subtitles too (a default path is used if None specified)
        """
        filepath = os.path.normpath(filepath)
        self.projectfile = self.GetProjectFilepath(filepath or "subtitles")
        if self.projectfile == filepath and not self.read_project:
            self.read_project = True
            self.write_project = True

        # Check if the project file exists
        if self.read_project and not os.path.exists(self.projectfile):
            logging.info(f"Project file {self.projectfile} does not exist")
            self.read_project = False
            self.load_subtitles = True

        if self.read_project:
            # Try to load the project file
            subtitles = self.ReadProjectFile(self.projectfile)

            if subtitles and subtitles.scenes:
                self.load_subtitles = False
                if write_backup:
                    logging.info("Project file loaded, saving backup copy")
                    self.WriteBackupFile()
                else:
                    logging.info("Project file loaded")
            else:
                logging.error(f"Unable to read project file, starting afresh")
                self.load_subtitles = True

        if self.load_subtitles:
            # (re)load the source subtitle file if required
            subtitles = self.LoadSubtitleFile(filepath)

        if outputpath:
            subtitles.outputpath = outputpath

        if not subtitles.has_subtitles:
            raise ValueError(f"No subtitles to translate in {filepath}")

    def SaveTranslation(self, outputpath : str = None):
        """
        Write output file
        """
        try:
            self.subtitles.SaveTranslation(outputpath)

        except Exception as e:
            logging.error(f"Unable to save translation: {e}")

    def AnyTranslated(self):
        """
        Have any subtitles been translated yet?
        """
        return True if self.subtitles and self.subtitles.translated else False

    def GetProjectFilepath(self, filepath):
        path, ext = os.path.splitext(filepath)
        filepath = filepath if ext == '.subtrans' else f"{path}.subtrans"
        return os.path.normpath(filepath)
    
    def GetBackupFilepath(self, filepath):
        projectfile = self.GetProjectFilepath(filepath)
        return f"{projectfile}-backup"
    
    def LoadSubtitleFile(self, filepath):
        """
        Load subtitles from an SRT file
        """
        with self.lock:
            self.subtitles = SubtitleFile(filepath)
            self.subtitles.LoadSubtitles()
            self.subtitles.project = self

        return self.subtitles

    def WriteProjectFile(self, projectfile = None):
        """
        Write a set of subtitles to a project file
        """
        with self.lock:
            if not self.subtitles:
                raise Exception("Can't write project file, no subtitles")

            if not isinstance(self.subtitles, SubtitleFile):
                raise Exception("Can't write project file, wrong content type")

            if not self.subtitles.scenes:
                raise Exception("Can't write project file, no scenes")

            if projectfile and not self.write_project:
                self.write_project = True
                self.read_project = True

            if not projectfile:
                projectfile = self.projectfile
            elif projectfile and not self.projectfile:
                self.projectfile = self.GetProjectFilepath(projectfile)
                self.subtitles.outputpath = GetOutputPath(projectfile, self.subtitles.target_language)

            if not projectfile:
                raise Exception("No file path provided")
            
            projectfile = os.path.normpath(projectfile)

            logging.info(f"Writing project data to {str(projectfile)}")

            with open(projectfile, 'w', encoding=default_encoding) as f:
                project_json = json.dumps(self.subtitles, cls=SubtitleEncoder, ensure_ascii=False, indent=4)
                f.write(project_json)
            
            self.needsupdate = False

    def WriteBackupFile(self):
        """
        Save a backup copy of the project
        """
        if self.subtitles and self.projectfile:
            backupfile = self.GetBackupFilepath(self.projectfile)
            self.WriteProjectFile(backupfile)

    def ReadProjectFile(self, filepath = None):
        """
        Load scenes, subtitles and context from a project file
        """
        try:
            filepath = filepath or self.projectfile
            with self.lock:
                logging.info(f"Reading project data from {str(filepath)}")

                with open(filepath, 'r', encoding=default_encoding, newline='') as f:
                    subtitles: SubtitleFile = json.load(f, cls=SubtitleDecoder)

                subtitles.Sanitise()
                subtitles.project = self
                self.subtitles = subtitles
                return subtitles

        except FileNotFoundError:
            logging.error(f"Project file {filepath} not found")
            return None

        except json.JSONDecodeError as e:
            logging.error(f"Error decoding JSON file: {e}")
            return None

    def UpdateProjectFile(self):
        """
        Write current state of scenes to the project file
        """
        if self.update_project:
            if not self.subtitles:
                raise Exception("Unable to update project file, no subtitles")
            
            self.needsupdate = True

    def GetProjectSettings(self):
        """
        Return a dictionary of non-empty settings from the project file
        """
        return { key : value for key, value in self.subtitles.settings.items() if value }

    def UpdateProjectSettings(self, settings: dict):
        """
        Replace settings if the provided dictionary has an entry with the same key
        """
        if isinstance(settings, Options):
            settings = settings.options

        with self.lock:
            if not self.subtitles:
                return

            if all(settings.get(key) == self.subtitles.settings.get(key) for key in settings):
                return

            self.subtitles.UpdateProjectSettings(settings)

        if self.subtitles.scenes:
            self.subtitles.UpdateOutputPath()
            self.WriteProjectFile()

    def TranslateSubtitles(self, translator : SubtitleTranslator):
        """
        Use the translation provider to translate a project
        """
        if not self.subtitles:
            raise Exception("No subtitles to translate")
        
        # Prime new project files
        if self.write_project:
            self.WriteProjectFile()

        try:
            translator.events.preprocessed += self._on_preprocessed
            translator.events.batch_translated += self._on_batch_translated
            translator.events.scene_translated += self._on_scene_translated

            translator.TranslateSubtitles(self.subtitles)

            translator.events.preprocessed -= self._on_preprocessed
            translator.events.batch_translated -= self._on_batch_translated
            translator.events.scene_translated -= self._on_scene_translated

            self.SaveTranslation()

        except TranslationAbortedError:
            logging.warning(f"Translation aborted")
            raise

        except Exception as e:
            if self.subtitles and translator.stop_on_error:
                self.SaveTranslation()

            logging.error(f"Failed to translate subtitles")
            raise

    def TranslateScene(self, translator : SubtitleTranslator, scene_number : int, batch_numbers : list[int] = None, line_numbers : list[int] = None):
        """
        Pass batches of subtitles to the translation engine.
        """
        if not self.subtitles:
            raise Exception("No subtitles to translate")

        try:
            translator.events.preprocessed += self._on_preprocessed
            translator.events.batch_translated += self._on_batch_translated

            scene = self.subtitles.GetScene(scene_number)

            translator.TranslateScene(self.subtitles, scene, batch_numbers=batch_numbers, line_numbers=line_numbers)

            translator.events.preprocessed -= self._on_preprocessed
            translator.events.batch_translated -= self._on_batch_translated

            self.SaveTranslation()

            return scene
        
        except TranslationAbortedError:
            raise

        except Exception as e:
            if self.subtitles and translator.stop_on_error:
                self.SaveTranslation()

            logging.error(f"Failed to translate subtitles")
            raise

    def _start_autosave_thread(self):
        self.stop_event = threading.Event()
        self.periodic_update_thread = threading.Thread(target=self._background_autosave)
        self.periodic_update_thread.daemon = True
        self.periodic_update_interval = 20  # Autosave interval in seconds
        self.periodic_update_thread.start()

    def _background_autosave(self):
        while not self.stop_event.is_set():
            if self.needsupdate:
                self.needsupdate = False
                self.WriteProjectFile()
            self.stop_event.wait(self.periodic_update_interval)

    def _on_preprocessed(self, scenes):
        logging.debug("Pre-processing finished")
        self.needsupdate = self.update_project
        self.events.preprocessed(scenes)

    def _on_batch_translated(self, batch):
        logging.debug("Batch translated")
        self.needsupdate = self.update_project
        self.events.batch_translated(batch)

    def _on_scene_translated(self, scene):
        logging.debug("Scene translated")
        self.SaveTranslation()
        self.needsupdate = self.update_project
        self.events.scene_translated(scene)
