/* File-transfer driver for experiments. Timing ends at the receiver's receipt,
 * before FIN/TIME_WAIT; protocol behavior remains entirely in the library. */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include "cmu_tcp.h"

static double seconds(void) {
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  return now.tv_sec + now.tv_nsec / 1e9;
}

static int read_exact(cmu_socket_t* sock, void* data, size_t length) {
  unsigned char* cursor = data;
  while (length) {
    int count = cmu_read(sock, cursor, (int)length, NO_FLAG);
    if (count <= 0) return -1;
    cursor += count;
    length -= count;
  }
  return 0;
}

int main(int argc, char** argv) {
  if (argc != 5 || (strcmp(argv[1], "send") && strcmp(argv[1], "receive"))) {
    fprintf(stderr, "usage: %s send|receive host port file\n", argv[0]);
    return 2;
  }
  char* end;
  long port = strtol(argv[3], &end, 10);
  if (*end || port < 1 || port > 65535) return 2;
  int sending = !strcmp(argv[1], "send");
  FILE* file = fopen(argv[4], sending ? "rb" : "wb");
  if (!file) {
    perror("fopen");
    return 1;
  }
  cmu_socket_t sock;
  if (cmu_socket(&sock, sending ? TCP_INITIATOR : TCP_LISTENER, (int)port,
                 argv[2]) < 0) {
    perror("cmu_socket");
    return 1;
  }
  unsigned char header[8], receipt[8], buffer[32768];
  uint64_t size = 0;
  double started = seconds();
  if (sending) {
    struct stat info;
    if (fstat(fileno(file), &info) || info.st_size < 0) return 1;
    size = (uint64_t)info.st_size;
    for (int i = 0; i < 8; ++i) header[i] = size >> (56 - 8 * i);
    if (cmu_write(&sock, header, sizeof(header)) < 0) return 1;
  } else {
    puts("READY");
    fflush(stdout);
    if (read_exact(&sock, header, sizeof(header))) return 1;
    for (int i = 0; i < 8; ++i) size = (size << 8) | header[i];
  }
  uint64_t remaining = size;
  while (remaining) {
    size_t count =
        remaining < sizeof(buffer) ? (size_t)remaining : sizeof(buffer);
    if (sending) {
      if (fread(buffer, 1, count, file) != count ||
          cmu_write(&sock, buffer, (int)count) < 0)
        return 1;
    } else if (read_exact(&sock, buffer, count) ||
               fwrite(buffer, 1, count, file) != count)
      return 1;
    remaining -= count;
  }
  if (fclose(file)) return 1;
  if (sending) {
    if (read_exact(&sock, receipt, sizeof(receipt)) ||
        memcmp(header, receipt, sizeof(header)))
      return 1;
    double elapsed = seconds() - started;
    printf("{\"bytes\":%" PRIu64 ",\"seconds\":%.9f,\"mbps\":%.9f}\n", size,
           elapsed, size * 8.0 / elapsed / 1e6);
    fflush(stdout);
  } else if (cmu_write(&sock, header, sizeof(header)) < 0)
    return 1;
  return cmu_close(&sock) < 0 ? 1 : 0;
}
