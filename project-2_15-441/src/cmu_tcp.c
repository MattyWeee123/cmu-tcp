/**
 * Copyright (C) 2022 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * No part of the project may be copied and/or distributed without the express
 * permission of the 15-441/641 course staff.
 *
 *
 * This file implements the high-level API for CMU-TCP sockets.
 */

#include "cmu_tcp.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "backend.h"

int buffer_append(cmu_buffer_t* buffer, const void* data, size_t length) {
  if (length > INT_MAX - buffer->length) {
    errno = ENOBUFS;
    return EXIT_ERROR;
  }
  size_t needed = buffer->length + length;
  if (buffer->offset + needed > buffer->capacity) {
    if (needed > buffer->capacity) {
      size_t capacity = buffer->capacity ? buffer->capacity : 4096;
      while (capacity < needed) capacity *= 2;
      uint8_t* resized = realloc(buffer->data, capacity);
      if (resized == NULL) return EXIT_ERROR;
      buffer->data = resized;
      buffer->capacity = capacity;
    }
    if (buffer->length > 0)
      memmove(buffer->data, buffer->data + buffer->offset, buffer->length);
    buffer->offset = 0;
  }
  if (length > 0)
    memcpy(buffer->data + buffer->offset + buffer->length, data, length);
  buffer->length = needed;
  return EXIT_SUCCESS;
}

void buffer_consume(cmu_buffer_t* buffer, size_t length) {
  buffer->offset += length;
  buffer->length -= length;
  if (buffer->length == 0) buffer->offset = 0;
}

/* A full pipe already represents a pending wakeup, so EAGAIN is harmless. */
static void wake_backend(cmu_socket_t* sock) {
  const uint8_t byte = 1;
  ssize_t result;
  do {
    result = write(sock->wake_pipe[1], &byte, sizeof(byte));
  } while (result < 0 && errno == EINTR);
}

static int random_sequence(uint32_t* seq) {
  int fd = open("/dev/urandom", O_RDONLY);
  if (fd < 0) return EXIT_ERROR;
  size_t offset = 0;
  while (offset < sizeof(*seq)) {
    ssize_t count = read(fd, (uint8_t*)seq + offset, sizeof(*seq) - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) {
      int error = count == 0 ? EIO : errno;
      close(fd);
      errno = error;
      return EXIT_ERROR;
    }
    offset += (size_t)count;
  }
  close(fd);
  return EXIT_SUCCESS;
}

int cmu_socket(cmu_socket_t* sock, const cmu_socket_type_t socket_type,
               const int port, const char* server_ip) {
  if (sock == NULL || port < 0 || port > UINT16_MAX ||
      (socket_type != TCP_INITIATOR && socket_type != TCP_LISTENER) ||
      (socket_type == TCP_INITIATOR && (server_ip == NULL || port == 0))) {
    errno = EINVAL;
    return EXIT_ERROR;
  }
  memset(sock, 0, sizeof(*sock));
  sock->socket = -1;
  sock->wake_pipe[0] = sock->wake_pipe[1] = -1;
  sock->type = socket_type;
  sock->conn.sin_family = AF_INET;
  sock->conn.sin_port = htons((uint16_t)port);
  if (socket_type == TCP_INITIATOR &&
      inet_pton(AF_INET, server_ip, &sock->conn.sin_addr) != 1) {
    errno = EINVAL;
    return EXIT_ERROR;
  }

  int error = pthread_mutex_init(&sock->lock, NULL);
  if (error != 0) {
    errno = error;
    return EXIT_ERROR;
  }
  error = pthread_cond_init(&sock->wait_cond, NULL);
  if (error != 0) {
    pthread_mutex_destroy(&sock->lock);
    errno = error;
    return EXIT_ERROR;
  }
  if (random_sequence(&sock->initial_seq) < 0) goto fail;
  sock->window.last_ack_received = sock->initial_seq;
  sock->window.next_seq_to_send = sock->initial_seq;
  sock->socket = socket(AF_INET, SOCK_DGRAM, 0);
  if (sock->socket < 0) goto fail;
  int socket_flags = fcntl(sock->socket, F_GETFL);
  if (socket_flags < 0 ||
      fcntl(sock->socket, F_SETFL, socket_flags | O_NONBLOCK) < 0)
    goto fail;

  struct sockaddr_in local = {0};
  local.sin_family = AF_INET;
  local.sin_addr.s_addr = htonl(INADDR_ANY);
  local.sin_port = socket_type == TCP_LISTENER ? htons((uint16_t)port) : 0;
  if (socket_type == TCP_LISTENER) {
    int reuse = 1;
    if (setsockopt(sock->socket, SOL_SOCKET, SO_REUSEADDR, &reuse,
                   sizeof(reuse)) < 0)
      goto fail;
  }
  if (bind(sock->socket, (struct sockaddr*)&local, sizeof(local)) < 0)
    goto fail;
  socklen_t local_len = sizeof(local);
  if (getsockname(sock->socket, (struct sockaddr*)&local, &local_len) < 0)
    goto fail;
  sock->my_port = ntohs(local.sin_port);

  if (pipe(sock->wake_pipe) < 0) goto fail;
  for (int i = 0; i < 2; ++i) {
    int flags = fcntl(sock->wake_pipe[i], F_GETFL);
    if (flags < 0 || fcntl(sock->wake_pipe[i], F_SETFL, flags | O_NONBLOCK) < 0)
      goto fail;
  }
  error = pthread_create(&sock->thread_id, NULL, begin_backend, sock);
  if (error != 0) {
    errno = error;
    goto fail;
  }
  return EXIT_SUCCESS;

fail:
  error = errno;
  if (sock->socket >= 0) close(sock->socket);
  if (sock->wake_pipe[0] >= 0) close(sock->wake_pipe[0]);
  if (sock->wake_pipe[1] >= 0) close(sock->wake_pipe[1]);
  pthread_cond_destroy(&sock->wait_cond);
  pthread_mutex_destroy(&sock->lock);
  sock->socket = -1;
  errno = error;
  return EXIT_ERROR;
}

int cmu_close(cmu_socket_t* sock) {
  if (sock == NULL || sock->socket < 0) {
    errno = EINVAL;
    return EXIT_ERROR;
  }
  pthread_mutex_lock(&sock->lock);
  sock->dying = 1;
  pthread_mutex_unlock(&sock->lock);
  wake_backend(sock);

  int error = pthread_join(sock->thread_id, NULL);
  if (error != 0) {
    errno = error;
    return EXIT_ERROR;
  }
  error = sock->error;
  if (close(sock->socket) < 0 && error == 0) error = errno;
  close(sock->wake_pipe[0]);
  close(sock->wake_pipe[1]);
  free(sock->receive_buffer.data);
  free(sock->send_buffer.data);
  pthread_cond_destroy(&sock->wait_cond);
  pthread_mutex_destroy(&sock->lock);
  sock->socket = -1;
  if (error != 0) {
    errno = error;
    return EXIT_ERROR;
  }
  return EXIT_SUCCESS;
}

int cmu_read(cmu_socket_t* sock, void* buf, int length, cmu_read_mode_t flags) {
  if (sock == NULL || sock->socket < 0 || length < 0 ||
      (length > 0 && buf == NULL) || (flags != NO_FLAG && flags != NO_WAIT)) {
    errno = EINVAL;
    return EXIT_ERROR;
  }
  if (length == 0) return 0;
  pthread_mutex_lock(&sock->lock);
  while (flags == NO_FLAG && sock->receive_buffer.length == 0 &&
         !sock->backend_done)
    pthread_cond_wait(&sock->wait_cond, &sock->lock);

  size_t count = sock->receive_buffer.length;
  if (count > (size_t)length) count = (size_t)length;
  if (count > 0) {
    memcpy(buf, sock->receive_buffer.data + sock->receive_buffer.offset, count);
    buffer_consume(&sock->receive_buffer, count);
  }
  int error = sock->error;
  pthread_mutex_unlock(&sock->lock);
  if (count == 0 && error != 0) {
    errno = error;
    return EXIT_ERROR;
  }
  return (int)count;
}

int cmu_write(cmu_socket_t* sock, const void* buf, int length) {
  if (sock == NULL || sock->socket < 0 || length < 0 ||
      (length > 0 && buf == NULL)) {
    errno = EINVAL;
    return EXIT_ERROR;
  }
  pthread_mutex_lock(&sock->lock);
  if (sock->dying || sock->backend_done) {
    int error = sock->error ? sock->error : EPIPE;
    pthread_mutex_unlock(&sock->lock);
    errno = error;
    return EXIT_ERROR;
  }
  int result = buffer_append(&sock->send_buffer, buf, (size_t)length);
  pthread_mutex_unlock(&sock->lock);
  if (result == EXIT_SUCCESS && length > 0) wake_backend(sock);
  return result;
}
