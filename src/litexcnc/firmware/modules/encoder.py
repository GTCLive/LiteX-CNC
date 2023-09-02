import math

# Imports for creating a LiteX/Migen module
from litex.soc.interconnect.csr import *
from migen import *
from litex.soc.integration.soc import SoC
from litex.soc.integration.doc import AutoDoc, ModuleDoc
from litex.build.generic_platform import *


class EncoderModule(Module, AutoDoc):
    """Hardware counting of quadrature encoder signals.

    Encoder is used to measure position by counting the pulses generated by a
    quadrature encoder. For each pulse it takes 3 clock-cycles for the FPGA to
    process the signal. For a 50 MHz FPGA, this would result in a roughly 15
    MHz count rate as a theoretical upper limit.

    One also should take into account that the Z-signal has to be processed by
    LinuxCNC. Given a 1 kHz servo-thread (perios 1000 ns), this would lead to an
    upper limit of 60000 RPM (1000 Hz) for the encoder. That's really fast, but
    gives with a 2500 PPR encoder a merely 2.5 MHz count-rate...

    The counters used in this application are signed 32-bit fields. This means
    that at a count-rate of 2.5 MHz (which is deemed the real practical upper
    limit). The counter will overflow in 858 seconds (just shy of 15 minutes)
    in case it is running at top speed and it is not reset.
    """
    pads_layout = [("Encoder_A", 1), ("Encoder_B", 1), ("Encoder_Z", 1)]

    COUNTER_SIZE = 32

    def __init__(self, encoder_config: 'EncoderInstanceConfig', pads=None) -> None:

        # AutoDoc implementation
        self.intro = ModuleDoc(self.__class__.__doc__)
        # Require to test working with Verilog, basically creates extra signals not
        # connected to any pads.
        if pads is None:
            pads = Record(self.pads_layout)
        self.pads = pads

        # Exported pins
        self.pin_A = Signal()
        self.pin_B = Signal()
        pin_Z = Signal()

        # Exported fields
        self.index_enable = Signal()
        self.counter = Signal((self.COUNTER_SIZE, True), reset=encoder_config.reset_value)
        self.index_pulse = Signal()
        self.reset_index_pulse = Signal()
        self.reset = Signal()

        # Internal fields
        pin_A_delayed = Signal(3)
        pin_B_delayed = Signal(3)
        pin_Z_delayed = Signal(3)  # NOTE: Z is delayed for 2 cycles (0,1) the third postion is
                                   # used to detect rising edges.
        self.count_ena = Signal()
        self.count_dir = Signal()

        # Program
        # - Create the connections to the pads
        self.comb += [
            self.pin_A.eq(pads.Encoder_A),
            self.pin_B.eq(pads.Encoder_B),
        ]
        # - Add support for Z-index if pin is defined. If not, the Signal is set to be constant
        if hasattr(pads, 'Encoder_Z'):
            self.comb += pin_Z.eq(pads.Encoder_Z)
        else:
            self.comb += pin_Z.eq(Constant(0))
        # - In most cases, the "quadX" signals are not synchronous to the FPGA clock. The
        #   classical solution is to use 2 extra D flip-flops per input to avoid introducing
        #   metastability into the counter (src: https://www.fpga4fun.com/QuadratureDecoder.html)
        self.comb += [
            self.count_ena.eq(pin_A_delayed[1] ^ pin_A_delayed[2] ^ pin_B_delayed[1] ^ pin_B_delayed[2]),
            self.count_dir.eq(pin_A_delayed[1] ^ pin_B_delayed[2])
        ]
        self.sync += [
            pin_A_delayed.eq(Cat(self.pin_A, pin_A_delayed[:2])),
            pin_B_delayed.eq(Cat(self.pin_B, pin_B_delayed[:2])),
            pin_Z_delayed.eq(Cat(pin_Z, pin_Z_delayed[:2])),
            # Storing the index pulse (detection of raising flank)
            If(
                pin_Z_delayed[1] & ~pin_Z_delayed[2],
                self.index_pulse.eq(1)
            ),
            # Reset the index pulse as soon the CPU has indicated it is read
            If(
                self.reset_index_pulse & self.index_pulse,
                self.index_pulse.eq(encoder_config.reset_value),
                self.reset_index_pulse.eq(0)
            ),
            # When the `index-enable` flag is set, detext a raising flank and
            # reset the counter in that case
            If(
                self.reset | (self.index_enable & pin_Z_delayed[1] & ~pin_Z_delayed[2]),
                self.counter.eq(encoder_config.reset_value),
                self.index_enable.eq(0)
            ),
            # Counting implementation. Counting occurs when movement occcurs, but
            # not when the counter is reset by the `index-enable`. This takes into
            # account the corner-case when the reset and the count action happen
            # at exact the same clock-cycle, which (in simulations) showed the reset
            # would not happen.
            If(
                (pin_A_delayed[1] ^ pin_A_delayed[2] ^ pin_B_delayed[1] ^ pin_B_delayed[2]) & ~(self.index_enable & pin_Z_delayed[1] & ~pin_Z_delayed[2]),
                If(
                    pin_A_delayed[1] ^ pin_B_delayed[2], #self.count_dir,
                    self.create_counter_increase(encoder_config),
                ).Else(
                    self.create_counter_decrease(encoder_config)
                )
            )
        ]

        self.ios = {self.pin_A, self.pin_B}

    def create_counter_increase(self, encoder_config: 'EncoderInstanceConfig'):
        """
        Creates the statements for increasing the counter. When a maximum
        value for the counter is defined, this is taken into account.
        """
        if encoder_config.max_value is not None:
            return If(
                self.counter < encoder_config.max_value,
                self.counter.eq(self.counter + 1),
            )
        return self.counter.eq(self.counter + 1)

    def create_counter_decrease(self, encoder_config: 'EncoderInstanceConfig'):
        """
        Creates the statements for decreasing the counter. When a minimum
        value for the counter is defined, this is taken into account.
        """
        if encoder_config.min_value is not None:
            return If(
                self.counter > encoder_config.min_value,
                self.counter.eq(self.counter - 1),
            )
        return self.counter.eq(self.counter - 1)

    @classmethod
    def add_mmio_read_registers(cls, mmio, config: 'EncoderModuleConfig'):
        """
        Adds the status registers to the MMIO.

        NOTE: Status registers are meant to be read by LinuxCNC and contain
        the current status of the encoder.
        """
        # Don't create the registers when the config is empty (no encoders 
        # defined in this case)
        if not config:
            return
            
        # At least 1 encoder exits, create the registers.
        mmio.encoder_index_pulse = CSRStatus(
            size=int(math.ceil(float(len(config.instances))/32))*32,
            name='encoder_index_pulse',
            description="""Encoder index pulse
            Register containing the flags that an index pulse has been detected for the given
            encoder. After succefully reading this register, the index pulse should be reset
            by writing a 1 for the given encoder to the `reset index pulse`-register.
            """
        )
        for index in range(len(config.instances)):
            setattr(
                mmio,
                f'encoder_{index}_counter',
                CSRStatus(
                    size=cls.COUNTER_SIZE,
                    name=f'encoder_{index}_counter',
                    description="Encoder counter\n"
                    f"Register containing the count for register {index}."
                )
            )

    @classmethod
    def add_mmio_write_registers(cls, mmio, config: 'EncoderModuleConfig'):
        """
        Adds the storage registers to the MMIO.

        NOTE: Storage registers are meant to be written by LinuxCNC and contain
        the flags and configuration for the encoder.
        """
        # Don't create the registers when the config is empty (no encoders 
        # defined in this case)
        if not config:
            return

        # At least 1 encoder exits, create the registers.
        mmio.encoder_index_enable = CSRStorage(
            size=int(math.ceil(float(len(config.instances))/32))*32,
            name='encoder_index_enable', 
            description="""Index enable
            Register containing the `index enable`-flags. When true, the counter of the given
            encoder is reset to zero. This field has to be set for each index-pulse generated
            by the encoder.
            """, 
            write_from_dev=True)
        mmio.encoder_reset_index_pulse = CSRStorage(
            size=int(math.ceil(float(len(config.instances))/32))*32,
            name='encoder_reset_index_pulse',
            description="""Reset index pulse
            Register containing the detected index pulse should be cleared on the next clock
            cycle. Indicates the CPU has successfully read the index pulse from the card and
            has processed it.
            """
        )

    @classmethod
    def create_from_config(cls, soc: SoC, _, config: 'EncoderModuleConfig'):
        """
        Adds the encoders as defined in the configuration to the SoC.
        """
        # Don't create the module when the config is empty (no encoders 
        # defined in this case)
        if not config:
            return
        
        # At least 1 encoder exits, create the module(s).
        # - create a list of `index_pulse`-flags. These will be added later on
        #   outside the mainloop in a Cat-statement.
        index_pulse = []
        # - main loop for creating the encoders
        for index, instance_config in enumerate(config.instances):
            # Add the io to the FPGA
            if instance_config.pin_Z is not None:
                soc.platform.add_extension([
                    ("encoder", index,
                        Subsignal("Encoder_A", Pins(instance_config.pin_A), IOStandard(instance_config.io_standard)),
                        Subsignal("Encoder_B", Pins(instance_config.pin_B), IOStandard(instance_config.io_standard)),
                        Subsignal("Encoder_Z", Pins(instance_config.pin_Z), IOStandard(instance_config.io_standard))
                    )
                ])
            else:
                soc.platform.add_extension([
                    ("encoder", index,
                        Subsignal("Encoder_A", Pins(instance_config.pin_A), IOStandard(instance_config.io_standard)),
                        Subsignal("Encoder_B", Pins(instance_config.pin_B), IOStandard(instance_config.io_standard)),
                    )
                ])
            # Create the encoder
            pads = soc.platform.request("encoder", index)
            encoder = cls(encoder_config=instance_config, pads=pads)
            # Add the encoder to the soc
            soc.submodules += encoder
            # Hookup the ynchronous logic for transferring the data from the CPU to FPGA
            soc.sync += [
                # Reset the counter when LinuxCNC is started
                encoder.reset.eq(soc.MMIO_inst.reset.storage),
                # `index enable`-flag
                encoder.index_enable.eq(
                    soc.MMIO_inst.encoder_index_enable.storage[index]
                ),
                # `reset index pulse`-flag (indication data has been read by CPU)
                encoder.reset_index_pulse.eq(
                    soc.MMIO_inst.encoder_reset_index_pulse.storage[index]
                )
            ]
            soc.sync += encoder.index_enable.eq(soc.MMIO_inst.encoder_index_enable.storage[index])
            # Add combination logic for getting the status of the encoders
            soc.sync += getattr(soc.MMIO_inst, f"encoder_{index}_counter").status.eq(encoder.counter)
            # Add the index pulse flag to the output (if pin_Z is defined). Last step is to Cat this
            # list to a single output
            index_pulse.append(encoder.index_pulse if instance_config.pin_Z is not None else Constant(0))

        # Add combination logic for getting the  `index pulse`-flag. We have to use Cat here
        # so it is not possible to do this in the main loop.
        soc.comb += [
            soc.MMIO_inst.encoder_index_pulse.status.eq(Cat(index_pulse)),
        ]

if __name__ == "__main__":
    # Imports for creating a simulation
    from migen import *
    from migen.fhdl import *

    # Create a dummy config
    from litexcnc.config.modules.encoder import EncoderInstanceConfig
    config = EncoderInstanceConfig(
         pin_A="not_used_in_sim",
         pin_B="not_used_in_sim"
    )

    # Create encoder
    encoder = EncoderModule(config)
    
    # Create quadrature signal
    b = [1, 1, 0, 0]
    a = [0, 1, 1, 0]

    def test_encoder(encoder):

        for i in range(100):
            yield (encoder.pads.Encoder_A.eq(a[(i//2) % 4]))
            yield (encoder.pads.Encoder_B.eq(b[(i//2) % 4]))

            quad_a = (yield encoder.pin_A)
            quad_b = (yield encoder.pin_B)
            count_ena = (yield encoder.count_ena)
            count_dir = (yield encoder.count_dir)
            count = (yield encoder.counter)

            print(quad_a, quad_b, count_ena, count_dir, count)
            yield
        ...


    print("\nRunning Sim...\n")
    # print(verilog.convert(stepgen, stepgen.ios, "pre_scaler"))
    run_simulation(encoder, test_encoder(encoder))