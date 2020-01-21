import json
import os
import pickle

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from oauth2client.service_account import ServiceAccountCredentials
from tenacity import *

from bot import LOGGER, parent_id, DOWNLOAD_DIR, IS_TEAM_DRIVE, INDEX_URL, DOWNLOAD_STATUS_UPDATE_INTERVAL, \
    USE_SERVICE_ACCOUNTS
from bot.helper.ext_utils.bot_utils import *
from bot.helper.ext_utils.fs_utils import get_mime_type

logging.getLogger('googleapiclient.discovery').setLevel(logging.ERROR)

G_DRIVE_TOKEN_FILE = "token.pickle"
# Check https://developers.google.com/drive/scopes for all available scopes
OAUTH_SCOPE = ["https://www.googleapis.com/auth/drive"]

SERVICE_ACCOUNT_INDEX = 0


def authorize():
    # Get credentials
    credentials = None
    if not USE_SERVICE_ACCOUNTS:
        if os.path.exists(G_DRIVE_TOKEN_FILE):
            with open(G_DRIVE_TOKEN_FILE, 'rb') as f:
                credentials = pickle.load(f)
        if credentials is None or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', OAUTH_SCOPE)
                LOGGER.info(flow)
                credentials = flow.run_console(port=0)

            # Save the credentials for the next run
            with open(G_DRIVE_TOKEN_FILE, 'wb') as token:
                pickle.dump(credentials, token)
    else:
        credentials = ServiceAccountCredentials \
            .from_json_keyfile_name(f'accounts/{SERVICE_ACCOUNT_INDEX}.json')
    return build('drive', 'v3', credentials=credentials, cache_discovery=False)


service = authorize()


class GoogleDriveHelper:
    # Redirect URI for installed apps, can be left as is
    REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
    G_DRIVE_DIR_MIME_TYPE = "application/vnd.google-apps.folder"
    G_DRIVE_BASE_DOWNLOAD_URL = "https://drive.google.com/uc?id={}&export=download"

    def __init__(self, name=None, listener=None):
        self.__listener = listener
        self._file_uploaded_bytes = 0
        self.uploaded_bytes = 0
        self.start_time = 0
        self.total_time = 0
        self._should_update = True
        self.is_uploading = True
        self.is_cancelled = False
        self.status = None
        self.updater = None
        self.name = name

    def cancel(self):
        self.is_cancelled = True
        self.is_uploading = False

    def speed(self):
        """
        It calculates the average upload speed and returns it in bytes/seconds unit
        :return: Upload speed in bytes/second
        """
        try:
            return self.uploaded_bytes / self.total_time
        except ZeroDivisionError:
            return 0

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def _on_upload_progress(self):
        if self.status is not None:
            chunk_size = self.status.total_size * self.status.progress() - self._file_uploaded_bytes
            self._file_uploaded_bytes = self.status.total_size * self.status.progress()
            LOGGER.info(f'Chunk size: {get_readable_file_size(chunk_size)}')
            self.uploaded_bytes += chunk_size
            self.total_time += DOWNLOAD_STATUS_UPDATE_INTERVAL

    @staticmethod
    def __upload_empty_file(path, file_name, mime_type, parent_id=None):
        media_body = MediaFileUpload(path,
                                     mimetype=mime_type,
                                     resumable=False)
        file_metadata = {
            'name': file_name,
            'description': 'mirror',
            'mimeType': mime_type,
        }
        if parent_id is not None:
            file_metadata['parents'] = [parent_id]
        return service.files().create(supportsTeamDrives=True,
                                      body=file_metadata, media_body=media_body).execute()

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def __set_permission(self, drive_id):
        permissions = {
            'role': 'reader',
            'type': 'anyone',
            'value': None,
            'withLink': True
        }
        return service.permissions().create(supportsTeamDrives=True, fileId=drive_id, body=permissions).execute()

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def upload_file(self, file_path, file_name, mime_type, parent_id):
        global SERVICE_ACCOUNT_INDEX
        global service
        # File body description
        file_metadata = {
            'name': file_name,
            'description': 'mirror',
            'mimeType': mime_type,
        }
        if parent_id is not None:
            file_metadata['parents'] = [parent_id]

        if os.path.getsize(file_path) == 0:
            media_body = MediaFileUpload(file_path,
                                         mimetype=mime_type,
                                         resumable=False)
            response = service.files().create(supportsTeamDrives=True,
                                              body=file_metadata, media_body=media_body).execute()
            if not IS_TEAM_DRIVE:
                self.__set_permission(response['id'])
            drive_file = service.files().get(fileId=response['id']).execute()
            download_url = self.G_DRIVE_BASE_DOWNLOAD_URL.format(drive_file.get('id'))
            return download_url
        media_body = MediaFileUpload(file_path,
                                     mimetype=mime_type,
                                     resumable=True,
                                     chunksize=50 * 1024 * 1024)

        # Insert a file
        drive_file = service.files().create(supportsTeamDrives=True,
                                            body=file_metadata, media_body=media_body)
        response = None
        while response is None:
            if self.is_cancelled:
                return None
            try:
                self.status, response = drive_file.next_chunk()
            except HttpError as err:
                if err.resp.get('content-type', '').startswith('application/json'):
                    reason = json.loads(err.content).get('error').get('errors')[0].get('reason')
                    if reason == 'userRateLimitExceeded':
                        SERVICE_ACCOUNT_INDEX += 1
                        service = authorize()
                raise err
        self._file_uploaded_bytes = 0
        # Insert new permissions
        if not IS_TEAM_DRIVE:
            self.__set_permission(response['id'])
        # Define file instance and get url for download
        drive_file = service.files().get(supportsTeamDrives=True, fileId=response['id']).execute()
        download_url = self.G_DRIVE_BASE_DOWNLOAD_URL.format(drive_file.get('id'))
        return download_url

    def upload(self, file_name: str):
        self.__listener.onUploadStarted()
        file_dir = f"{DOWNLOAD_DIR}{self.__listener.message.message_id}"
        file_path = f"{file_dir}/{file_name}"
        LOGGER.info("Uploading File: " + file_path)
        self.start_time = time.time()
        self.updater = setInterval(5, self._on_upload_progress)
        if os.path.isfile(file_path):
            try:
                mime_type = get_mime_type(file_path)
                link = self.upload_file(file_path, file_name, mime_type, parent_id)
                if link is None:
                    raise Exception('Upload has been manually cancelled')
                LOGGER.info("Uploaded To G-Drive: " + file_path)
            except Exception as e:
                LOGGER.info(f"Total Attempts: {e.last_attempt.attempt_number}")
                LOGGER.error(e.last_attempt.exception())
                self.__listener.onUploadError(e)
                return
            finally:
                self.updater.cancel()
        else:
            try:
                dir_id = self.create_directory(os.path.basename(os.path.abspath(file_name)), parent_id)
                result = self.upload_dir(file_path, dir_id)
                if result is None:
                    raise Exception('Upload has been manually cancelled!')
                LOGGER.info("Uploaded To G-Drive: " + file_name)
                link = f"https://drive.google.com/folderview?id={dir_id}"
            except Exception as e:
                LOGGER.info(f"Total Attempts: {e.last_attempt.attempt_number}")
                LOGGER.error(e.last_attempt.exception())
                self.__listener.onUploadError(e)
                return
            finally:
                self.updater.cancel()
        LOGGER.info(download_dict)
        self.__listener.onUploadComplete(link)
        LOGGER.info("Deleting downloaded file/folder..")
        return link

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def create_directory(self, directory_name, parent_id):
        file_metadata = {
            "name": directory_name,
            "mimeType": self.G_DRIVE_DIR_MIME_TYPE
        }
        if parent_id is not None:
            file_metadata["parents"] = [parent_id]
        file = service.files().create(supportsTeamDrives=True, body=file_metadata).execute()
        file_id = file.get("id")
        if not IS_TEAM_DRIVE:
            self.__set_permission(file_id)
        LOGGER.info("Created Google-Drive Folder:\nName: {}\nID: {} ".format(file.get("name"), file_id))
        return file_id

    def upload_dir(self, input_directory, parent_id):
        list_dirs = os.listdir(input_directory)
        if len(list_dirs) == 0:
            return parent_id
        new_id = None
        for item in list_dirs:
            current_file_name = os.path.join(input_directory, item)
            if self.is_cancelled:
                return None
            if os.path.isdir(current_file_name):
                current_dir_id = self.create_directory(item, parent_id)
                new_id = self.upload_dir(current_file_name, current_dir_id)
            else:
                mime_type = get_mime_type(current_file_name)
                file_name = current_file_name.split("/")[-1]
                # current_file_name will have the full path
                self.upload_file(current_file_name, file_name, mime_type, parent_id)
                new_id = parent_id
        return new_id

    def drive_list(self, fileName):
        msg = ""
        # Create Search Query for API request.
        query = f"'{parent_id}' in parents and (name contains '{fileName}')"
        page_token = None
        results = []
        while True:
            response = service.files().list(supportsTeamDrives=True,
                                            includeTeamDriveItems=True,
                                            q=query,
                                            spaces='drive',
                                            fields='nextPageToken, files(id, name, mimeType, size)',
                                            pageToken=page_token,
                                            orderBy='modifiedTime desc').execute()
            for file in response.get('files', []):
                if len(results) >= 20:
                    break
                if file.get(
                        'mimeType') == "application/vnd.google-apps.folder":  # Detect Whether Current Entity is a Folder or File.
                    msg += f"⁍ <a href='https://drive.google.com/drive/folders/{file.get('id')}'>{file.get('name')}" \
                           f"</a> (folder)"
                    if INDEX_URL is not None:
                        url = f'{INDEX_URL}/{file.get("name")}/'
                        msg += f' | <a href="{url}"> Index URL</a>'
                else:
                    msg += f"⁍ <a href='https://drive.google.com/uc?id={file.get('id')}" \
                           f"&export=download'>{file.get('name')}</a> ({get_readable_file_size(int(file.get('size')))})"
                    if INDEX_URL is not None:
                        url = f'{INDEX_URL}/{file.get("name")}'
                        msg += f' | <a href="{url}"> Index URL</a>'
                msg += '\n'
                results.append(file)
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
        del results
        return msg
