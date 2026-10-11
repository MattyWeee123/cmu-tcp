/* Test-only read wrapper: make the initial sequence number reproducible when
 * requested, while preserving real reads for wake pipes and all other cases. */
#undef read
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

ssize_t cp1_test_read(int fd, void* buffer, size_t length) {
  ssize_t result = read(fd, buffer, length);
  const char* forced = getenv("CP1_TEST_INITIAL_SEQUENCE");
  if (forced != NULL && result == (ssize_t)sizeof(uint32_t) &&
      length == sizeof(uint32_t)) {
    uint32_t sequence = (uint32_t)strtoul(forced, NULL, 10);
    memcpy(buffer, &sequence, sizeof(sequence));
  }
  return result;
}
