"use client";

import Image from 'next/image';

import CustomButton from './CustomButton';

const Hero = () => {
  const handleScroll = () => {

  };
  
  return (
    <div className='hero'>
      <div className='flex-1 pt-36 padding-x'>
        <h1 className='hero__title'>
          Interview confidently with the help of AI!
        </h1>

        <p className='hero__subtitle'>
          We help you prepare for your interviews by providing you with the most common questions asked by top companies.
        </p>

        <CustomButton 
          title="Start Your Interview"
          containerStyles='bg-primary-blue text-white mt-10 rounded-full'
          handleClick={handleScroll} 
        />
      </div>
    </div>
  )
}

export default Hero