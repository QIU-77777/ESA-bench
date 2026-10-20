import asyncio
import io
import logging
import unittest
from unittest.mock import patch
from litellm import _logging as ll

class ViewerLoggingTests(unittest.TestCase):
    def test_original_context_survives_import_failure_and_redacts(self):
        for failure in (KeyError('litellm'),ImportError('module unavailable')):
            with self.subTest(failure=type(failure).__name__):
                stream=io.StringIO();handler=logging.StreamHandler(stream)
                logger=logging.getLogger('asyncio');logger.addHandler(handler)
                loop=asyncio.new_event_loop()
                try:
                    with patch.object(ll.diagnostics,'run',side_effect=failure):
                        loop.call_exception_handler({'message':'original async context','exception':RuntimeError('api_key=abcdefghijklmno')})
                    output=stream.getvalue()
                    self.assertIn('original async context',output)
                    self.assertIn('RuntimeError',output)
                    self.assertIn('REDACTED',output)
                    self.assertNotIn('abcdefghijklmno',output)
                    self.assertNotIn('Exception in default exception handler',output)
                finally:
                    logger.removeHandler(handler);loop.close()

if __name__=='__main__':unittest.main()
