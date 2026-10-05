package main

import (
	"fmt"
	"net/http"
)

// Server holds HTTP handlers.
type Server struct {
	Addr string
}

// Health reports liveness.
func (s *Server) Health(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintln(w, "ok")
}

func main() {
	s := &Server{Addr: ":8080"}
	http.HandleFunc("/health", s.Health)
	http.ListenAndServe(s.Addr, nil)
}
