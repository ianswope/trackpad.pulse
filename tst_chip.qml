import QtQuick
import QtTest

// The chip must fade a lifted touch whether or not the icon animation is on,
// and an idle chip with the animation off must never move (1.9.2, PR #3).
Rectangle {
  width: 300
  height: 200
  color: "#171b22"
  TrackpadChip { id: chip; x: 20; y: 20; width: 200; height: 130; animate: false }
  TestCase {
    name: "TrackpadChip"
    when: windowShown
    function swipeAndLift() {
      chip.fingers = [{slot: 0, x: 0.2, y: 0.8, p: null, palm: false, speed: 0}]
      wait(40)
      chip.fingers = [{slot: 0, x: 0.5, y: 0.8, p: null, palm: false, speed: 0}]
      wait(40)
      chip.fingers = [{slot: 0, x: 0.8, y: 0.8, p: null, palm: false, speed: 0}]
      wait(40)
      chip.fingers = []
    }
    function test_animation_off_trail_still_fades() {
      chip.animate = false
      swipeAndLift()
      verify(chip.busy, "trail present right after the lift")
      verify(Object.keys(chip.trails).length > 0, "trail points kept right after the lift")
      wait(1500)
      compare(Object.keys(chip.trails).length, 0, "trail aged out within 1.5 s with animation off")
      compare(chip.ripples.length, 0, "no ripple left")
      verify(!chip.busy, "chip idle again")
    }
    function test_animation_off_idle_phase_never_moves() {
      chip.animate = false
      wait(1600)
      var p = chip.phase
      wait(600)
      compare(chip.phase, p, "aura and sweep phase frozen with animation off")
    }
    function test_animation_on_still_fades_and_animates() {
      chip.animate = true
      var p = chip.phase
      swipeAndLift()
      wait(1500)
      compare(Object.keys(chip.trails).length, 0, "trail aged out with animation on")
      verify(chip.phase !== p, "phase advanced with animation on")
    }
  }
}
