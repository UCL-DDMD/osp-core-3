"""Test file upload and download."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import unittest2 as unittest

from osp.core.session.buffers import BufferContext
from osp.core.session.transport.communication_engine import (
    LEN_HEADER,
    CommunicationEngineServer,
)
from osp.core.session.transport.communication_utils import (
    BLOCK_SIZE,
    LEN_FILES_HEADER,
    decode_header,
    encode_files,
    encode_header,
    filter_files,
    receive_files,
    split_message,
)
from osp.core.session.transport.transport_session_client import (
    TransportSessionClient,
)
from osp.core.session.transport.transport_session_server import (
    TransportSessionServer,
)
from osp.core.session.transport.transport_utils import (
    check_hash,
    deserialize_buffers,
    get_file_cuds,
    get_hash,
    get_hash_dir,
    move_files,
    serialize_buffers,
)
from osp.wrappers.sqlite import SqliteSession

try:
    from .test_communication_engine import MockWebsocket, async_test
except Exception:
    from test_communication_engine import async_test, MockWebsocket

from osp.core.namespaces import cuba

try:
    from osp.core.namespaces import city
except ImportError:
    from osp.core.ontology import Parser
    from osp.core.ontology.namespace_registry import namespace_registry

    Parser().parse("city")
    city = namespace_registry.city

HOST = "127.0.0.1"
PORT = 8645
URI = f"ws://{HOST}:{PORT}"
DB = "filetransfer.db"

FILES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "filetransfer_files")
)
SECOND_FILES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "filetransfer_files_second")
)
CLIENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "filetransfer_client")
)
SERVER_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "filetransfer_server")
)
FILES = ["f0", "f1.jpg", "f2.tar.gz"]
FILE_PATHS = [os.path.join(FILES_DIR, file) for file in FILES]
SECOND_FILE_PATHS = [os.path.join(SECOND_FILES_DIR, file) for file in FILES]
HASHES = {
    FILES[
        0
    ]: "9722fa4a83e528278c7f5009da2486e7255af8756a888733a4ceaee449e0f102",
    FILES[
        1
    ]: "ccd521371b29352a7b02a04c2408c4e0ceacba97fc3ce449edd8897cb2397410",
    FILES[
        2
    ]: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


PRFX = "http://www.osp-core.com/cuds#00000000-0000-0000-0000-0000000000"
SERIALIZED_BUFFERS = json.dumps(
    {
        "added": [
            [
                {
                    "@id": PRFX + "2a",
                    "@type": ["http://www.osp-core.com/city#CityWrapper"],
                },
                {
                    "@id": PRFX + "03",
                    "http://www.osp-core.com/city#isPartOf": [
                        {"@id": PRFX + "2a"}
                    ],
                    "http://www.osp-core.com/cuba#path": [
                        {
                            "@type": "http://www.w3.org/2001/XMLSchema#string",
                            "@value": FILE_PATHS[2],
                        }
                    ],
                    "@type": ["http://www.osp-core.com/city#Image"],
                },
            ]
        ],
        "updated": [
            [
                {
                    "@id": PRFX + "2a",
                    "http://www.osp-core.com/city#hasPart": [
                        {"@id": PRFX + "01"},
                        {"@id": PRFX + "03"},
                    ],
                    "@type": ["http://www.osp-core.com/city#CityWrapper"],
                },
                {
                    "@id": PRFX + "01",
                    "@type": ["http://www.osp-core.com/city#Image"],
                },
                {
                    "@id": PRFX + "03",
                    "@type": ["http://www.osp-core.com/city#Image"],
                },
            ],
            [
                {
                    "@id": PRFX + "2a",
                    "@type": ["http://www.osp-core.com/city#CityWrapper"],
                },
                {
                    "@id": PRFX + "01",
                    "http://www.osp-core.com/city#isPartOf": [
                        {"@id": PRFX + "2a"}
                    ],
                    "http://www.osp-core.com/cuba#path": [
                        {
                            "@type": "http://www.w3.org/2001/XMLSchema#string",
                            "@value": FILE_PATHS[0],
                        }
                    ],
                    "@type": ["http://www.osp-core.com/city#Image"],
                },
            ],
        ],
        "deleted": [
            [
                {
                    "@id": PRFX + "02",
                    "@type": ["http://www.osp-core.com/city#Image"],
                }
            ]
        ],
        "expired": [],
    }
)


class TestFiletransfer(unittest.TestCase):
    """Test file upload and download."""

    SERVER_STARTED = False

    @classmethod
    def setUpClass(cls):
        """Set up the server as a subprocess."""
        args = ["python", "tests/test_filetransfer.py", "server"]
        p = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        TestFiletransfer.SERVER_STARTED = p
        for line in p.stdout:
            if b"ready" in line:
                time.sleep(0.1)
                break

    @classmethod
    def tearDownClass(cls):
        """Remove the database file."""
        TestFiletransfer.SERVER_STARTED.terminate()
        os.remove(DB)

    def tearDown(self):
        """Remove temporary directories and clear the database."""
        shutil.rmtree(FILES_DIR)
        shutil.rmtree(SECOND_FILES_DIR)
        shutil.rmtree(CLIENT_DIR)
        shutil.rmtree(SERVER_DIR)
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            tables = c.execute(
                "SELECT name FROM sqlite_master " + "WHERE type='table';"
            )
            tables = list(tables)
            for table in tables:
                c.execute("DELETE FROM `%s`;" % table[0])
            conn.commit()

    def setUp(self):
        """Set up some temporary directory and test files."""
        for directory in (FILES_DIR, SECOND_FILES_DIR, CLIENT_DIR, SERVER_DIR):
            if not os.path.exists(directory):
                os.mkdir(directory)
        with open(FILE_PATHS[0], "wb") as f:
            f.write(("0" * (BLOCK_SIZE + 1)).encode("utf-8"))
        with open(FILE_PATHS[1], "wb") as f:
            f.write(("1" * (BLOCK_SIZE * 2)).encode("utf-8"))
        with open(FILE_PATHS[2], "wb") as f:
            pass
        with open(SECOND_FILE_PATHS[0], "wb") as f:
            f.write(("0" * (BLOCK_SIZE + 1)).encode("utf-8"))  # Same
        with open(SECOND_FILE_PATHS[1], "wb") as f:
            f.write(("1" * (BLOCK_SIZE * 2 + 1)).encode("utf-8"))  # Different
        with open(SECOND_FILE_PATHS[2], "wb") as f:
            pass

    def test_move_files(self):
        """Test moving the files."""
        with TransportSessionClient(SqliteSession, URI) as session:
            # Image path is full path
            wrapper = city.CityWrapper(session=session)
            images = wrapper.add(
                city.Image(path=FILE_PATHS[0]),
                city.Image(path=FILE_PATHS[1]),
                city.Image(path=FILE_PATHS[2]),
            )
            result = move_files(images, None, CLIENT_DIR)
            target = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images, FILES)
            ]
            target_full_path = [os.path.join(CLIENT_DIR, t) for t in target]

            self.assertEqual(set(os.listdir(FILES_DIR)), set(FILES))
            self.assertEqual(set(os.listdir(CLIENT_DIR)), set(target))
            self.assertEqual(result, target_full_path)

            self.tearDown()
            self.setUp()

            # Image path is path on a different system
            paths = [
                FILES[0],
                os.path.join("foo", "bar", FILES[1]),
                os.path.abspath(".").split(os.path.sep)[0]
                + os.path.sep
                + FILES[2],
            ]
            images = wrapper.add(
                city.Image(path=paths[0]),
                city.Image(path=paths[1]),
                city.Image(path=paths[2]),
            )
            result = move_files(images, FILES_DIR, CLIENT_DIR)
            target = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images, FILES)
            ]
            target_full_path = [os.path.join(CLIENT_DIR, t) for t in target]
            self.assertEqual(set(os.listdir(CLIENT_DIR)), set(target))
            self.assertEqual(result, target_full_path)

            self.tearDown()
            self.setUp()

            # Not target given --> Nothing will be moved
            images = wrapper.add(
                city.Image(path=paths[0]),
                city.Image(path=paths[1]),
                city.Image(path=paths[2]),
            )
            result = move_files(images, FILES_DIR, None)
            self.assertEqual(set(os.listdir(CLIENT_DIR)), set())
            self.assertEqual(result, paths)

            # Target does not exist
            images = wrapper.add(
                city.Image(path=paths[0]),
                city.Image(path=paths[1]),
                city.Image(path=paths[2]),
            )
            result = move_files(images, FILES_DIR, "not-existent")
            self.assertEqual(set(os.listdir(CLIENT_DIR)), set())
            self.assertEqual(result, list())

            # paths don't exist
            images = wrapper.add(
                city.Image(path=paths[0]),
                city.Image(path=paths[1]),
                city.Image(path=paths[2]),
            )
            result = move_files(images, None, CLIENT_DIR)
            self.assertEqual(set(os.listdir(CLIENT_DIR)), set())
            self.assertEqual(result, list())

    def setup_buffers1(self, session):
        """Set up the buffers for the tests below."""
        wrapper = city.CityWrapper(session=session)
        images = wrapper.add(
            city.Image(path=FILE_PATHS[0]), city.Image(path=FILE_PATHS[1])
        )
        session._reset_buffers(BufferContext.USER)
        wrapper.remove(images[1].uid)
        images[0].path = FILE_PATHS[0]
        images = list(images) + [wrapper.add(city.Image(path=FILE_PATHS[2]))]
        session.prune()
        return images

    def test_serialize_buffers(self):
        """Test correct handling of files when serializing the buffers."""
        # without providing target path
        with TransportSessionClient(SqliteSession, URI) as session:
            self.setup_buffers1(session)
            _, result = serialize_buffers(
                session,
                buffer_context=BufferContext.USER,
                target_directory=None,
            )
            self.assertEqual(
                sorted(map(os.path.abspath, [FILE_PATHS[0], FILE_PATHS[2]])),
                sorted(result),
            )

        # provide target path --> move files
        with TransportSessionClient(SqliteSession, URI) as session:
            images = self.setup_buffers1(session)
            _, result = serialize_buffers(
                session,
                buffer_context=BufferContext.USER,
                target_directory=CLIENT_DIR,
            )
            target = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images, FILES)
            ]
            target_full_path = [os.path.join(CLIENT_DIR, t) for t in target]
            self.assertEqual(
                sorted([target_full_path[0], target_full_path[2]]),
                sorted(result),
            )
            self.maxDiff = None

    def setup_buffers2(self, session):
        """Set up the buffers for the tests below."""
        wrapper = city.CityWrapper(session=session, uid=42)
        images = wrapper.add(
            city.Image(path=FILE_PATHS[0], uid=1),
            city.Image(path=FILE_PATHS[1], uid=2),
        )
        session._reset_buffers(BufferContext.USER)
        return images

    def test_deserialize_buffers(self):
        """Test correct file handling when deserializing buffers."""
        with TransportSessionClient(SqliteSession, URI) as session:
            images = self.setup_buffers2(session)
            deserialize_buffers(
                session,
                buffer_context=BufferContext.USER,
                data=SERIALIZED_BUFFERS,
                temp_directory=None,
                target_directory=CLIENT_DIR,
            )
            added, updated, deleted = session._buffers[BufferContext.USER]
            self.assertEqual(len(added), 1)
            self.assertEqual(len(updated), 2)
            self.assertEqual(len(deleted), 1)
            images = images + [added[uuid.UUID(int=3)]]
            target = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images, FILES)
            ]
            target_full_path = [os.path.join(CLIENT_DIR, t) for t in target]
            self.assertEqual(added[uuid.UUID(int=3)].path, target_full_path[2])
            self.assertEqual(
                updated[uuid.UUID(int=1)].path, target_full_path[0]
            )
            self.assertRaises(
                AttributeError, getattr, deleted[uuid.UUID(int=2)], "path"
            )

    def test_get_file_cuds(self):
        """Test extracting the file cuds from a datatstructure."""
        image1 = city.Image(path="x")
        image2 = city.Image(path="y")
        c = city.City(name="Freiburg")
        x = {"a": image1, "b": 1, "c": None, "d": [c, image2]}
        r = get_file_cuds(x)
        self.assertEqual(r, [image1, image2])

    def test_encode_files(self):
        """Test encoding of files."""
        result = encode_files(FILE_PATHS)
        self.maxDiff = None
        r = next(result)
        num_blocks, filename = decode_header(r, LEN_FILES_HEADER)
        self.assertEqual(num_blocks, 2)
        self.assertEqual(filename, FILES[0])
        r = next(result)
        self.assertEqual(r, ("0" * BLOCK_SIZE).encode("utf-8"))
        r = next(result)
        self.assertEqual(r, "0".encode("utf-8"))
        r = next(result)
        num_blocks, filename = decode_header(r, LEN_FILES_HEADER)
        self.assertEqual(num_blocks, 2)
        self.assertEqual(filename, FILES[1])
        r = next(result)
        self.assertEqual(r, ("1" * BLOCK_SIZE).encode("utf-8"))
        r = next(result)
        self.assertEqual(r, ("1" * BLOCK_SIZE).encode("utf-8"))
        r = next(result)
        num_blocks, filename = decode_header(r, LEN_FILES_HEADER)
        self.assertEqual(num_blocks, 0)
        self.assertEqual(filename, FILES[2])
        self.assertRaises(StopIteration, next, result)

    @async_test
    async def test_receive_files(self):
        """Test receiving files via file transfer."""
        ws = MockWebsocket(
            id=0,
            to_recv=[
                encode_header([2, FILES[0]], LEN_FILES_HEADER),
                ("0" * BLOCK_SIZE).encode("utf-8"),
                "0".encode("utf-8"),
                encode_header([2, FILES[1]], LEN_FILES_HEADER),
                ("1" * BLOCK_SIZE).encode("utf-8"),
                ("1" * BLOCK_SIZE).encode("utf-8"),
                encode_header([0, FILES[2]], LEN_FILES_HEADER),
            ],
            sent_data=[],
        )
        file_hashes = dict()
        await receive_files(3, ws, SERVER_DIR, file_hashes)
        self.assertEqual({k: x for k, x in file_hashes.items()}, HASHES)
        self.assertEqual(sorted(os.listdir(SERVER_DIR)), sorted(FILES))
        self.assertEqual(get_hash_dir(SERVER_DIR), HASHES)

    def setup_buffers3(self, session):
        """Set up the buffers for the upload and download tests."""
        wrapper = city.CityWrapper(session=session)
        images = wrapper.add(
            city.Image(path=FILE_PATHS[0]),
            city.Image(path=FILE_PATHS[1]),
            city.Image(path=FILE_PATHS[1]),
        )
        images_second = wrapper.add(
            city.Image(path=SECOND_FILE_PATHS[0]),
            city.Image(path=SECOND_FILE_PATHS[1]),
        )
        session._reset_buffers(BufferContext.USER)
        wrapper.remove(images[1].uid)
        images[0].path = FILE_PATHS[0]
        images[2].path = FILE_PATHS[1]
        images_second[0].path = SECOND_FILE_PATHS[0]
        images_second[1].path = SECOND_FILE_PATHS[1]
        images = [list(images)[0], list(images)[2]] + [
            wrapper.add(city.Image(path=FILE_PATHS[2]))
        ]
        session.prune()
        return images, images_second

    def test_upload(self):
        """Test full upload routine."""
        # with given file destination on client
        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            images, images_second = self.setup_buffers3(session)
            session.commit()
            target = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images, FILES)
            ]
            target_second = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images_second, FILES)
            ]
            self.assertEqual(
                set(os.listdir(SERVER_DIR)),
                {
                    target[0],
                    target[1],
                    target[2],
                    target_second[0],
                    target_second[1],
                },
            )
            self.assertEqual(
                set(os.listdir(CLIENT_DIR)),
                {
                    FILES[0],
                    FILES[1],
                    FILES[2],
                    os.path.splitext(FILES[1])[0]
                    + f" ({images_second[1].uid})"
                    + os.path.splitext(FILES[1])[1],
                },
            )
            self.assertEqual(set(os.listdir(FILES_DIR)), set(FILES))
            self.assertEqual(set(os.listdir(SECOND_FILES_DIR)), set(FILES))

        self.tearDown()
        self.setUp()

        # With no given file destination on client
        with TransportSessionClient(
            SqliteSession, URI, file_destination=None
        ) as session:
            images, images_second = self.setup_buffers3(session)
            session.commit()
            target = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images, FILES)
            ]
            target_second = [
                "%s-%s" % (image.uid.hex, file)
                for image, file in zip(images_second, FILES)
            ]
            self.assertEqual(
                set(os.listdir(SERVER_DIR)),
                {
                    target[0],
                    target[1],
                    target[2],
                    target_second[0],
                    target_second[1],
                },
            )
            self.assertEqual(set(os.listdir(CLIENT_DIR)), set())
            self.assertEqual(set(os.listdir(FILES_DIR)), set(FILES))
            self.assertEqual(set(os.listdir(SECOND_FILES_DIR)), set(FILES))

    def test_download(self):
        """Test full download routine."""
        with TransportSessionClient(
            SqliteSession, URI, file_destination=None
        ) as session:
            images = self.setup_buffers1(session)
            session.commit()

        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            city.CityWrapper(session=session)
            session.load(images[0].uid)
            session.load(images[1].uid)
            session.load(images[2].uid)
            self.assertEqual(set(os.listdir(CLIENT_DIR)), {FILES[0], FILES[2]})

        # download again and check that no errors occur
        # and that the duplicates are still
        # in the download folder (and not more)
        number_of_downloaded_files = len(os.listdir(CLIENT_DIR))
        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            city.CityWrapper(session=session)
            session.load(images[0].uid)
            session.load(images[1].uid)
            session.load(images[2].uid)
            self.assertEqual(
                number_of_downloaded_files, len(os.listdir(CLIENT_DIR))
            )

    def test_path(self):
        """Tests that the path of the CUBA file on the client is correct.

        Created due to issue #652.
        """
        # Upload file and retrieve twice. Both paths should be correct.
        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            wrapper = city.CityWrapper(session=session)
            file = cuba.File(path=FILE_PATHS[1])  # `f1.jpg`
            wrapper.add(file)
            session.commit()

        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            wrapper = city.CityWrapper(session=session)
            file = wrapper.get(file.uid)
            path1 = file.path

        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            wrapper = city.CityWrapper(session=session)
            file = wrapper.get(file.uid)
            path2 = file.path

        self.assertEqual(os.path.dirname(path1), CLIENT_DIR)
        self.assertEqual(os.path.dirname(path2), CLIENT_DIR)

        # Upload two identical instances of the same file, also upload two
        # different files with the same name.
        # The CLIENT_DIR is not cleaned up on purpose.
        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            wrapper = city.CityWrapper(session=session)
            file1 = cuba.File(path=FILE_PATHS[0])  # `f0`
            file2 = cuba.File(path=FILE_PATHS[0])  # `f0`
            file3 = cuba.File(path=FILE_PATHS[1])  # `f1.jpg`
            file4 = cuba.File(path=SECOND_FILE_PATHS[1])  # `f1.jpg` (second
            # file)
            wrapper.add(file1, file2, file3, file4)
            session.commit()

        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            wrapper = city.CityWrapper(session=session)
            path1 = wrapper.get(file1.uid).path
            path2 = wrapper.get(file2.uid).path
            path3 = wrapper.get(file3.uid).path
            path4 = wrapper.get(file4.uid).path

        self.assertTrue(
            all(
                path.startswith(CLIENT_DIR)
                for path in (path1, path2, path3, path4)
            ),
            True,
        )
        self.assertEqual(path1, path2)
        self.assertNotEqual(path3, path4)

        # Now clean up the folder (make sure that the server can find the
        # files, that is, the path is correct for the server).
        for file in os.listdir(CLIENT_DIR):
            os.remove(os.path.join(CLIENT_DIR, file))
        with TransportSessionClient(
            SqliteSession, URI, file_destination=CLIENT_DIR
        ) as session:
            wrapper = city.CityWrapper(session=session)
            path1 = wrapper.get(file1.uid).path
            path2 = wrapper.get(file2.uid).path
            path3 = wrapper.get(file3.uid).path
            path4 = wrapper.get(file4.uid).path

        self.assertTrue(
            all(
                path.startswith(CLIENT_DIR)
                for path in (path1, path2, path3, path4)
            ),
            True,
        )
        self.assertEqual(path1, path2)
        self.assertNotEqual(path3, path4)

    def test_hashes(self):
        """Test the methods for computing hashes."""
        self.assertEqual(get_hash_dir(FILES_DIR), HASHES)
        self.assertEqual(get_hash_dir(CLIENT_DIR), {})
        self.assertEqual(
            get_hash(FILE_PATHS[0]),
            "9722fa4a83e528278c7f5009da2486e7255af8756a888733a4ceaee449e0f102",
        )
        hashes = dict(HASHES)
        self.assertTrue(check_hash(FILE_PATHS[0], HASHES))
        self.assertFalse(check_hash(__file__, hashes))
        self.assertIn(os.path.basename(__file__), hashes)
        self.assertFalse(check_hash(FILE_PATHS[0], {}))

    def test_filter_files(self):
        """Test filtering files based on hashes."""
        self.assertEqual(filter_files(FILE_PATHS, HASHES), [])
        self.assertEqual(filter_files(FILE_PATHS, {}), FILE_PATHS)
        self.assertEqual(
            filter_files(FILE_PATHS + [__file__, "x"], dict(HASHES)),
            [__file__],
        )

    @async_test
    async def test_serve(self):
        """Test serve method of the server."""
        request = None
        response = []

        def handle_request(command, data, temp_directory, connection_id):
            nonlocal request
            request = (command, data, temp_directory, connection_id)
            return "response", FILE_PATHS

        s = CommunicationEngineServer(
            host=None,
            port=None,
            handle_request=handle_request,
            handle_disconnect=lambda u: None,
        )
        ws = MockWebsocket(
            id=0,
            to_recv=[
                encode_header([1, 4, 2, "test"], LEN_HEADER),
                *split_message("data", block_size=1)[1],
                encode_header([2, FILES[0]], LEN_FILES_HEADER),
                ("0" * BLOCK_SIZE).encode("utf-8"),
                "0".encode("utf-8"),
                encode_header([2, FILES[1]], LEN_FILES_HEADER),
                ("1" * BLOCK_SIZE).encode("utf-8"),
                ("1" * BLOCK_SIZE).encode("utf-8"),
            ],
            sent_data=response,
        )
        await s._serve(ws, None)
        self.assertEqual(list(s._file_hashes.keys()), [])
        self.assertEqual(list(s._file_hashes.values()), [])
        version, num_blocks, num_files = decode_header(response[0], LEN_HEADER)
        self.assertEqual(version, 1)
        self.assertEqual(num_blocks, 1)
        self.assertEqual(num_files, 1)
        self.assertEqual(response[1], b"response")
        num_blocks, filename = decode_header(response[2], LEN_FILES_HEADER)
        self.assertEqual(num_blocks, 0)
        self.assertEqual(filename, FILES[2])
        self.assertEqual(request[0], "test")
        self.assertEqual(request[1], "data")
        # TODO: Think of a different way to perform the check below. The
        #  condition does is not always true. For example if the IDE is
        #  installed as a Flatpak package, then the temporary directory has a
        #  different path format:
        #  /run/user/1000/app/com.jetbrains.PyCharm-Community/tmpgdjc38pd.
        # if os.name == "posix":
        #     self.assertTrue(request[2].startswith("/tmp/tmp"))
        self.assertTrue(isinstance(request[3], uuid.UUID))


if __name__ == "__main__":
    if sys.argv[-1] == "server":
        server = TransportSessionServer(
            SqliteSession,
            HOST,
            PORT,
            session_kwargs={"path": DB},
            file_destination=SERVER_DIR,
        )
        print("ready", flush=True)
        server.startListening()
    else:
        unittest.main()
