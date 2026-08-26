// The same HTTP server as ../graceful, with the signal handling removed.
//
// http.ListenAndServe blocks in the main goroutine and nothing is watching for
// SIGTERM. This is the shape of a service written by someone who has not had to
// think about shutdown yet: correct under load, silent about termination.
package main

import (
	"log"
	"net/http"
	"time"
)

const workDuration = 5 * time.Second

func routes() *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ready"}`))
	})

	// The signal is aimed 100ms in, so this is the whole margin the fixture
	// has: whatever is left of the handler after T0 is what SP005 counts as
	// in flight. One second left only 900ms of it, close enough to a loaded
	// runner's scheduling noise that the window occasionally measured empty
	// and SP005 reported nothing_in_flight instead of the destruction this
	// row exists to prove.
	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(workDuration)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"done"}`))
	})

	return mux
}

func main() {
	log.Println("listening on :8000 (no signal handling)")
	log.Fatal(http.ListenAndServe(":8000", routes()))
}
