outer_loop: LOOP
  inner_loop: LOOP
    LEAVE inner_loop;
  END LOOP inner_loop;
  LEAVE outer_loop;
END LOOP outer_loop
